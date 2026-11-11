"""API requests: retries, rate limits and lapsed logins, the bootstrap pages,
SSL verification, and the browser streamrip presents itself as."""

import asyncio
import base64
import re
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import certifi
import pytest

from streamrip.client import client as client_module
from streamrip.client import new_session
from streamrip.client.client import (
    _IDENTITIES,
    MAX_API_ATTEMPTS,
    MAX_RETRY_DELAY,
    RATE_LIMIT_BUDGET,
    RATE_LIMIT_PAUSE,
    SLOWDOWN_EASING,
    SLOWDOWN_MAX,
    SLOWDOWN_START,
    _sec_ch_ua,
    api_headers,
    browser_headers,
    register_header_dict,
)
from streamrip.client.qobuz import QobuzClient, QobuzSpoofer
from streamrip.client.soundcloud import SoundcloudClient
from streamrip.client.tidal import TidalClient
from streamrip.config import Config
from streamrip.exceptions import AccountHalt, AuthenticationError, ItemNotFoundError
from streamrip.media.playlist import PendingLastfmPlaylist
from streamrip.rip.cli import coro, latest_streamrip_version
from streamrip.utils.ssl_utils import get_aiohttp_connector_kwargs

TOKEN = "SECRET-TOKEN-abcdefghij"


class _Response:
    def __init__(
        self, status=200, body=None, headers=None, content_type="application/json"
    ):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self.url = "https://api.example/v1/tracks/1"
        self._body = body if body is not None else {"ok": True}

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                MagicMock(real_url=self.url), (), status=self.status
            )

    async def json(self, content_type=None):
        return self._body

    async def text(self, encoding="utf-8"):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Plays back scripted outcomes: a response, or an exception to raise."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def close(self):
        self.closed = True

    def get(self, url, params=None, headers=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _CountingLimiter:
    def __init__(self):
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1

    async def __aexit__(self, *exc):
        return False


def _client(*outcomes, cls=TidalClient):
    c = cls(Config.defaults())
    c.rate_limiter = _CountingLimiter()
    c.session = _Session(*outcomes)
    return c


class _Clock:
    """client_module's time, moved by hand or by its own sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.slept.append(delay)
        self.now += delay


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(client_module, "time", clock)
    return clock


@pytest.fixture
def sleeps(clock, monkeypatch):
    """asyncio.sleep without the wait: the delays asked for."""
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    return clock.slept


@pytest.fixture(autouse=True)
def identity_state(monkeypatch):
    """A fresh browser identity, no deadline served yet, nothing registered."""
    monkeypatch.setattr(client_module, "_identity", {})
    monkeypatch.setattr(client_module, "_rotate_at", 0.0)
    monkeypatch.setattr(client_module, "_header_dicts", [])


# --- retries ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "result"),
    [
        (TidalClient, {"id": 1}),
        (QobuzClient, (200, {"id": 1})),  # with its status, whatever it is
        (SoundcloudClient, (200, {"id": 1})),
    ],
    ids=["tidal", "qobuz", "soundcloud"],
)
@pytest.mark.parametrize(
    "failures",
    [
        [aiohttp.ServerDisconnectedError()],
        [TimeoutError()],
        [_Response(503), _Response(502, content_type="text/html")],  # an edge's page
    ],
    ids=["dropped", "timeout", "5xx"],
)
async def test_a_passing_failure_is_retried_through_the_limiter(
    sleeps, cls, result, failures
):
    c = _client(*failures, _Response(body={"id": 1}), cls=cls)
    assert await c._api_request("tracks/1", {}) == result
    assert c.session.calls == c.rate_limiter.entered == len(failures) + 1
    assert len(sleeps) == len(failures)


@pytest.mark.parametrize(
    ("failure", "raised"),
    [
        (aiohttp.ServerDisconnectedError(), "Server disconnected"),
        (_Response(503), "503"),
    ],
)
async def test_the_last_attempts_failure_is_raised(sleeps, failure, raised):
    c = _client(*[failure] * MAX_API_ATTEMPTS)
    with pytest.raises(aiohttp.ClientError, match=raised):
        await c._api_request("tracks/1")
    assert c.session.calls == MAX_API_ATTEMPTS
    assert len(sleeps) == MAX_API_ATTEMPTS - 1


@pytest.mark.parametrize(
    ("outcome", "raised"),
    [
        (_Response(404), ItemNotFoundError),
        # Without a renewal behind it, a 401 is just another client error.
        (_Response(401), aiohttp.ClientResponseError),
        (
            aiohttp.ClientSSLError(MagicMock(), OSError("bad certificate")),
            aiohttp.ClientSSLError,
        ),
    ],
    ids=["404", "401", "ssl"],
)
async def test_what_is_not_retried(sleeps, outcome, raised):
    c = _client(outcome)
    c._renew_login = AsyncMock(return_value=False)
    with pytest.raises(raised):
        await c._api_request("tracks/1")
    assert c.session.calls == 1
    assert sleeps == []


async def test_qobuz_client_errors_are_returned_not_retried(sleeps):
    c = _client(_Response(401, body={"message": "bad token"}), cls=QobuzClient)
    assert await c._api_request("user/login", {}) == (401, {"message": "bad token"})
    assert c.session.calls == 1


async def test_qobuz_retry_warning_does_not_quote_the_token(sleeps, caplog):
    c = _client(aiohttp.ServerDisconnectedError(), _Response(), cls=QobuzClient)
    await c._api_request("user/login", {"user_id": "1", "user_auth_token": TOKEN})
    assert "retrying" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("pause_during", ["sleep", "admission"])
async def test_pause_extensions_require_fresh_admission(monkeypatch, pause_during):
    c = _client(*[_Response(503)] * 3, _Response(), cls=QobuzClient)
    # Also exercise clients sharing another client's account pause.
    owner = _client(cls=QobuzClient)
    owner._retry_at = 5.0
    monkeypatch.setattr(c, "_pause_owner", lambda: owner)
    now = 0.0
    extensions = 0
    events = []

    def extend_pause():
        nonlocal extensions
        if extensions < MAX_API_ATTEMPTS:
            owner._retry_at = now + 10
            extensions += 1

    class Limiter:
        active = False

        async def __aenter__(self):
            assert now >= owner._retry_at
            self.active = True
            events.append("admit")
            if pause_during == "admission":
                extend_pause()

        async def __aexit__(self, *exc):
            self.active = False
            events.append("exit")

    async def sleep(delay):
        nonlocal now
        assert not c.rate_limiter.active
        events.append("sleep")
        now += delay
        if pause_during == "sleep":
            extend_pause()

    get = c.session.get

    def checked_get(*args, **kwargs):
        assert now >= owner._retry_at
        assert c.rate_limiter.active
        assert events[-1] == "admit"
        events.append("send")
        return get(*args, **kwargs)

    c.rate_limiter = Limiter()
    monkeypatch.setattr(c.session, "get", checked_get)
    monkeypatch.setattr(
        client_module, "time", SimpleNamespace(monotonic=lambda: now, time=lambda: now)
    )
    monkeypatch.setattr(asyncio, "sleep", sleep)

    assert await c._api_request("track/get", {}) == (200, {"ok": True})
    assert c.session.calls == MAX_API_ATTEMPTS
    assert extensions == MAX_API_ATTEMPTS
    if pause_during == "admission":
        assert events[:13] == ["sleep", "admit", "exit"] * 4 + ["sleep"]


# --- rate limits --------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "pause"),
    [
        ({"Retry-After": "7"}, 7),
        ({}, RATE_LIMIT_PAUSE),
        ({"Retry-After": "3600"}, MAX_RETRY_DELAY),
    ],
    ids=["retry-after", "none", "capped"],
)
async def test_a_429_pauses_the_account_then_spreads_its_requests_out(
    sleeps, headers, pause
):
    c = _client(_Response(429, headers=headers), _Response(), _Response())
    await c._api_request("tracks/1")
    # +0-2s jitter, so concurrent requests don't all wake and re-trip it together.
    assert len(sleeps) == 1
    assert pause <= sleeps[0] < pause + 2

    await c._api_request("tracks/2")  # the pause is over, the spacing is not
    assert c.session.calls == 3
    assert sleeps[1:] == pytest.approx([SLOWDOWN_START])


async def test_every_new_rate_limit_doubles_the_spacing_up_to_a_cap(sleeps):
    c = _client(*[_Response(429), _Response()] * 7)
    spacings = []
    for _ in range(7):
        await c._api_request("tracks/1")
        spacings.append(c._spacing)
    assert spacings[:3] == pytest.approx(
        [x * SLOWDOWN_EASING for x in (2, 4, 8)], rel=0.1
    )
    assert spacings[-1] == pytest.approx(SLOWDOWN_MAX * SLOWDOWN_EASING)


async def test_the_spacing_eases_off_while_requests_go_through(sleeps):
    c = _client(*[_Response()] * 100)
    c._spacing = SLOWDOWN_START
    for _ in range(100):
        await c._api_request("tracks/1")
    assert c._spacing == 0


@pytest.mark.parametrize(
    ("earlier", "stops"), [(0.0, True), (-10_000.0, False)], ids=["now", "long ago"]
)
async def test_too_many_rate_limits_in_the_hour_stop_the_run(sleeps, earlier, stops):
    c = _client(_Response(429), _Response())
    c._limited_at = (earlier,) * (RATE_LIMIT_BUDGET - 1)
    if stops:
        with pytest.raises(AccountHalt, match="Paused to protect your account"):
            await c._api_request("tracks/1")
        # Every request after it stops too, without being sent.
        with pytest.raises(AccountHalt):
            await c._api_request("tracks/2")
        assert c.session.calls == 1
    else:
        await c._api_request("tracks/1")
        assert len(c._limited_at) == 1  # only this one counts


def test_the_stop_gets_past_the_handlers_that_log_and_go_on():
    assert not issubclass(AccountHalt, Exception)


# --- a login that lapses mid-run ----------------------------------------------


@pytest.mark.parametrize("again", [200, 401], ids=["renewed", "renewed only once"])
async def test_a_lapsed_login_is_renewed_and_the_request_sent_again(sleeps, again):
    c = _client(_Response(401), _Response(again, body={"id": 1}))
    c._renew_login = AsyncMock(return_value=True)
    if again == 200:
        assert await c._api_request("tracks/1") == {"id": 1}
    else:
        with pytest.raises(aiohttp.ClientResponseError):
            await c._api_request("tracks/1")
    assert c.session.calls == 2
    c._renew_login.assert_awaited_once()


@pytest.mark.parametrize(
    ("sub_status", "refreshed"), [(11003, True), (11002, True), (4005, False)]
)
async def test_only_a_401_about_the_login_refreshes_it(sleeps, sub_status, refreshed):
    # 11003 and 11002: an expired or invalid token; 4005: a track not playable.
    body = {"status": 401, "subStatus": sub_status, "userMessage": "..."}
    c = _client(_Response(401, body=body), _Response(body={"id": 1}))
    c._refresh_access_token = AsyncMock()

    if refreshed:
        assert await c._api_request("tracks/1") == {"id": 1}
    else:
        with pytest.raises(aiohttp.ClientResponseError):
            await c._api_request("tracks/1")
    assert c._refresh_access_token.await_count == refreshed


async def test_a_renewal_on_the_last_attempt_still_ends_the_retries(sleeps):
    # The renewed 401 used up attempt 4, and `attempt == 4` never held again.
    errors = [_Response(503)] * (MAX_API_ATTEMPTS + 5)
    c = _client(*errors[: MAX_API_ATTEMPTS - 1], _Response(401), *errors)
    c._renew_login = AsyncMock(return_value=True)
    with pytest.raises(aiohttp.ClientResponseError):
        await c._api_request("tracks/1")
    assert c.session.calls == MAX_API_ATTEMPTS + 1


async def test_renew_login_refreshes_at_most_once_a_minute(monkeypatch):
    """Requests in flight together all hit the expiry at once; they retry on
    the one refresh, so the refresh token rotates only once."""
    c = _client()
    refresh = AsyncMock()
    monkeypatch.setattr(c, "_refresh_access_token", refresh)
    assert await c._renew_login()
    assert await c._renew_login()  # a sibling, in the same minute
    assert refresh.await_count == 1


async def test_a_refused_renewal_stops_the_run(monkeypatch):
    """A suspended account refuses the refresh: every track after it would
    ask again and be refused too."""
    c = _client()
    refused = AsyncMock(
        side_effect=AuthenticationError("Tidal refused the token refresh: suspended")
    )
    monkeypatch.setattr(c, "_refresh_access_token", refused)
    with pytest.raises(AccountHalt, match=r"login failed mid-run: .*suspended"):
        await c._renew_login()
    # A sibling that was waiting for the renewal does not ask again, and no
    # request after it is sent.
    with pytest.raises(AccountHalt):
        await c._renew_login()
    with pytest.raises(AccountHalt):
        await c._api_request("tracks/1")
    assert refused.await_count == 1
    assert c.session.calls == 0


# --- the pages a client bootstraps from ---------------------------------------

QOBUZ_PAGE = '<script src="/resources/1.2.3-a123/bundle.js"></script>'
QOBUZ_BUNDLE = 'production:{api:{appId:"123456789",appSecret:"' + "x" * 32 + '"'
QOBUZ_SEEDS = "".join(
    f'x.initialSeed("{base64.b64encode(timezone.encode()).decode()}{"A" * 44}",'
    f"window.utimezone.{timezone})"
    for timezone in ("paris", "london")
)


@pytest.mark.parametrize("provider", ["qobuz", "soundcloud"])
@pytest.mark.parametrize("failure_at", [0, 1], ids=["page", "bundle"])
@pytest.mark.parametrize("failure", [TimeoutError(), _Response(503)])
async def test_bootstrap_requests_retry_and_use_the_account_limiter(
    monkeypatch, sleeps, provider, failure_at, failure
):
    if provider == "qobuz":
        client = _client(cls=QobuzClient)
        page, bundle = QOBUZ_PAGE, QOBUZ_BUNDLE + QOBUZ_SEEDS
        expected = ("123456789", ["london", "paris"])
        fetch = client._get_app_id_and_secrets
    else:
        client = _client(cls=SoundcloudClient)
        page, bundle = '<script src="/bundle.js"></script>', f'client_id:"{"a" * 32}"'
        expected = ("a" * 32, "")
        fetch = client._refresh_tokens

    outcomes = [_Response(body=page), _Response(body=bundle)]
    outcomes.insert(failure_at, failure)
    session = _Session(*outcomes)
    if provider == "qobuz":
        monkeypatch.setattr("streamrip.client.qobuz.new_session", lambda **_: session)
    else:
        client.session = session

    assert await fetch() == expected
    assert session.calls == client.rate_limiter.entered == 3
    if provider == "qobuz":
        assert session.closed


async def test_bootstrap_does_not_parse_a_persistent_http_error(sleeps):
    c = _client(*[_Response(503)] * MAX_API_ATTEMPTS, cls=QobuzClient)
    with pytest.raises(aiohttp.ClientResponseError, match="503"):
        await c._get_text_with_retries("https://example.test/bundle.js")
    assert c.session.calls == MAX_API_ATTEMPTS


async def test_a_bundle_without_seeds_fails_with_a_clear_error(monkeypatch):
    """A changed bundle used to end in an opaque IndexError."""
    session = _Session(_Response(body=QOBUZ_PAGE), _Response(body=QOBUZ_BUNDLE))
    monkeypatch.setattr("streamrip.client.qobuz.new_session", lambda **_: session)

    with pytest.raises(Exception, match="app secrets"):
        await _client(cls=QobuzClient)._get_app_id_and_secrets()


# --- SSL verification ---------------------------------------------------------


def test_verification_is_one_shared_context_of_the_certifi_bundle():
    context = get_aiohttp_connector_kwargs()["ssl"]
    certifi_only = ssl.create_default_context(cafile=certifi.where())
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats() == certifi_only.cert_store_stats()
    assert get_aiohttp_connector_kwargs()["ssl"] is context
    assert get_aiohttp_connector_kwargs(verify_ssl=False) == {"ssl": False}


@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_sessions_follow_verify_ssl(verify_ssl):
    # Qobuz's spoofer took verify_ssl and then verified anyway.
    async with (
        new_session(verify_ssl=verify_ssl) as session,
        QobuzSpoofer(verify_ssl=verify_ssl) as spoofer,
    ):
        for s in (session, spoofer.session):
            # aiohttp keeps what a connection is checked with here.
            assert isinstance(s.connector._ssl, ssl.SSLContext) is verify_ssl


async def test_lastfm_session_follows_verify_ssl():
    config = MagicMock()
    config.session.downloads.verify_ssl = False
    playlist = PendingLastfmPlaylist("url", MagicMock(), None, config, MagicMock())
    with (
        patch(
            "streamrip.media.playlist.new_session", side_effect=RuntimeError("stop")
        ) as make,
        pytest.raises(RuntimeError),
    ):
        await playlist._parse_lastfm_playlist("https://www.last.fm/x")
    make.assert_called_once_with(verify_ssl=False)


async def test_update_check_follows_verify_ssl_and_is_never_fatal():
    with patch(
        "streamrip.rip.cli.new_session", side_effect=RuntimeError("offline")
    ) as make:
        _, notes, _, _, _ = await latest_streamrip_version(verify_ssl=False)
    assert notes is None
    assert make.call_args.kwargs["verify_ssl"] is False


def test_certificate_error_in_any_command_prints_help(capsys):
    # Only `url` and `file` used to catch it; the others crashed.
    error = aiohttp.ClientConnectorCertificateError(
        MagicMock(), ssl.SSLCertVerificationError("self-signed certificate")
    )

    @coro
    async def command():
        raise error

    with pytest.raises(SystemExit) as ended:
        command()
    assert ended.value.code == 1
    assert "--no-ssl-verify" in capsys.readouterr().out


# --- the browser identity -----------------------------------------------------


# What Chromium 155, 156 and 157 send, read from measured captures of real
# builds (157 computed with Chromium's own recipe, which reproduces the
# measured versions exactly). Brave puts its own brand in the same list.
@pytest.mark.parametrize(
    ("major", "brand", "expected"),
    [
        (155, "Google Chrome", '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"'),
        (156, "Google Chrome", '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"'),
        (157, "Google Chrome", '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"'),
        (156, "Brave", '"Not:A-Brand";v="8", "Chromium";v="156", "Brave";v="156"'),
    ],
)  # fmt: skip
def test_sec_ch_ua_is_chromiums_for_that_version(major, brand, expected):
    assert _sec_ch_ua(major, brand) == expected


@pytest.mark.parametrize("identity", _IDENTITIES)
def test_every_identity_agrees_with_itself(identity):
    """A Windows user agent never claims macOS in its hints, and the brand
    list follows the user agent's version: a contradiction between the two
    is exactly what gives a fake browser away."""
    match = re.fullmatch(
        r"Mozilla/5\.0 \(([^)]+)\) AppleWebKit/537\.36 \(KHTML, like Gecko\) "
        r"Chrome/(\d+)\.0\.0\.0 Safari/537\.36",
        identity["User-Agent"],
    )
    assert match is not None
    os_token, major = match.group(1), int(match.group(2))

    token_to_platform = {
        token: platform for platform, token in client_module._UA_PLATFORMS.items()
    }
    platform = token_to_platform[os_token]
    assert identity["Sec-CH-UA-Platform"] == f'"{platform}"'
    # Brave's user agent is Chrome's; only its brand in the list differs.
    assert identity["Sec-CH-UA"] in {_sec_ch_ua(major), _sec_ch_ua(major, "Brave")}
    assert identity["Sec-CH-UA-Mobile"] == "?0"
    assert re.fullmatch(
        r"[a-z]{2,3}(-[A-Z]{2})?(,[a-z]{2,3}(-[A-Z]{2})?(;q=[0-9.]+)?)*",
        identity["Accept-Language"],
    )
    assert set(identity) == {
        "User-Agent",
        "Sec-CH-UA",
        "Sec-CH-UA-Mobile",
        "Sec-CH-UA-Platform",
        "Accept-Language",
    }


async def test_sessions_follow_the_identity_as_it_rotates(clock):
    async with (
        new_session() as followed,
        new_session(user_agent="streamrip/test") as own_agent,
    ):
        before = dict(followed.headers)
        assert before == browser_headers()
        assert before in _IDENTITIES

        clock.now += 3 * 60 * 60  # past any identity's time

        assert browser_headers() != before
        assert browser_headers() in _IDENTITIES
        assert dict(followed.headers) == browser_headers()
        # An explicit user agent replaces the identity, rotations and all.
        assert dict(own_agent.headers) == {"User-Agent": "streamrip/test"}


async def test_api_calls_carry_the_fetch_headers_downloads_do_not():
    headers = api_headers({"authorization": "Bearer t"})

    assert headers["Sec-Fetch-Site"] == "same-site"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Sec-Fetch-Dest"] == "empty"
    assert headers["authorization"] == "Bearer t"
    assert set(browser_headers()).isdisjoint({"Sec-Fetch-Site"})


def test_a_registered_header_dict_follows_but_keeps_its_own_language(clock):
    headers = {"User-Agent": "Chrome/79", "Accept-Language": "fr_FR"}
    register_header_dict(headers)

    clock.now += 3 * 60 * 60

    assert headers.pop("Accept-Language") == "fr_FR"
    assert headers == {k: v for k, v in api_headers().items() if k != "Accept-Language"}
