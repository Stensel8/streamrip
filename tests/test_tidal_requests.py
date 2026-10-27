"""Tidal API requests survive a flaky network and back off from rate limits."""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from streamrip.client import client as client_module
from streamrip.client.client import (
    MAX_API_ATTEMPTS,
    MAX_RETRY_DELAY,
    RATE_LIMIT_BUDGET,
    RATE_LIMIT_PAUSE,
    SLOWDOWN_EASING,
    SLOWDOWN_MAX,
    SLOWDOWN_START,
)
from streamrip.client.tidal import TidalClient
from streamrip.config import Config
from streamrip.exceptions import AccountHalt, AuthenticationError, ItemNotFoundError


class _Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self.url = "https://api.tidalhifi.com/v1/tracks/1"
        self._body = body if body is not None else {"ok": True}

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                MagicMock(real_url=self.url), (), status=self.status
            )

    async def json(self):
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

    def get(self, url, params=None, headers=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def sleeps(monkeypatch):
    slept = []
    now = 0.0

    async def fake_sleep(delay):
        nonlocal now
        slept.append(delay)
        now += delay

    monkeypatch.setattr(
        client_module, "time", SimpleNamespace(monotonic=lambda: now, time=lambda: now)
    )
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def _client(*outcomes):
    c = TidalClient(Config.defaults())
    c.rate_limiter = contextlib.nullcontext()
    c.session = _Session(*outcomes)
    return c


async def test_connection_error_is_retried(sleeps):
    c = _client(aiohttp.ServerDisconnectedError(), _Response(body={"id": 1}))
    assert await c._api_request("tracks/1") == {"id": 1}
    assert c.session.calls == 2
    assert len(sleeps) == 1


async def test_timeout_is_retried(sleeps):
    c = _client(TimeoutError(), _Response())
    assert await c._api_request("tracks/1") == {"ok": True}
    assert c.session.calls == 2


async def test_server_error_is_retried(sleeps):
    c = _client(_Response(503), _Response(502), _Response(body={"id": 2}))
    assert await c._api_request("tracks/1") == {"id": 2}
    assert c.session.calls == 3


async def test_gives_up_after_the_last_attempt_and_raises(sleeps):
    c = _client(*[aiohttp.ServerDisconnectedError() for _ in range(MAX_API_ATTEMPTS)])
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await c._api_request("tracks/1")
    assert c.session.calls == MAX_API_ATTEMPTS
    assert len(sleeps) == MAX_API_ATTEMPTS - 1


async def test_persistent_server_error_raises_the_http_error(sleeps):
    c = _client(*[_Response(503) for _ in range(MAX_API_ATTEMPTS)])
    with pytest.raises(aiohttp.ClientResponseError) as e:
        await c._api_request("tracks/1")
    assert e.value.status == 503


async def test_rate_limit_waits_for_retry_after_and_pauses_later_requests(sleeps):
    c = _client(_Response(429, headers={"Retry-After": "7"}), _Response(), _Response())
    await c._api_request("tracks/1")
    # The account pause has elapsed before the next request is sent.
    await c._api_request("tracks/2")
    assert c.session.calls == 3
    # +0-2s jitter, so concurrent requests don't all wake and re-trip it together.
    assert 7 <= sleeps[0] < 9


async def test_rate_limit_without_retry_after_uses_a_default_pause(sleeps):
    c = _client(_Response(429), _Response())
    await c._api_request("tracks/1")
    assert len(sleeps) == 1
    assert RATE_LIMIT_PAUSE <= sleeps[0] < RATE_LIMIT_PAUSE + 2


async def test_absurd_retry_after_is_capped(sleeps):
    c = _client(_Response(429, headers={"Retry-After": "3600"}), _Response())
    await c._api_request("tracks/1")
    assert len(sleeps) == 1
    assert MAX_RETRY_DELAY <= sleeps[0] < MAX_RETRY_DELAY + 2


async def test_rate_limit_spreads_out_the_requests_that_follow(sleeps):
    c = _client(_Response(429), _Response(), _Response())
    await c._api_request("tracks/1")
    sleeps.clear()
    await c._api_request("tracks/2")
    assert sleeps == [SLOWDOWN_START]


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
    c = _client(*[_Response() for _ in range(100)])
    c._spacing = SLOWDOWN_START
    for _ in range(100):
        await c._api_request("tracks/1")
    assert c._spacing == 0


async def test_too_many_rate_limits_stop_the_run(sleeps):
    c = _client(_Response(429), _Response())
    c._limited_at = (0.0,) * (RATE_LIMIT_BUDGET - 1)
    with pytest.raises(AccountHalt, match="Stopped to protect your account"):
        await c._api_request("tracks/1")
    # Every request after it stops too, without being sent.
    with pytest.raises(AccountHalt):
        await c._api_request("tracks/2")
    assert c.session.calls == 1


async def test_rate_limits_from_long_ago_do_not_count(sleeps):
    c = _client(_Response(429), _Response())
    c._limited_at = (-10_000.0,) * RATE_LIMIT_BUDGET
    await c._api_request("tracks/1")
    assert len(c._limited_at) == 1


def test_the_stop_gets_past_the_handlers_that_log_and_go_on():
    assert not issubclass(AccountHalt, Exception)


async def test_not_found_is_not_retried(sleeps):
    c = _client(_Response(404))
    with pytest.raises(ItemNotFoundError):
        await c._api_request("tracks/1/lyrics")
    assert c.session.calls == 1
    assert sleeps == []


async def test_a_lapsed_login_is_renewed_and_the_request_retried(sleeps):
    c = _client(_Response(401), _Response(body={"id": 1}))
    c._renew_login = AsyncMock(return_value=True)
    assert await c._api_request("tracks/1") == {"id": 1}
    assert c.session.calls == 2
    c._renew_login.assert_awaited_once()


async def test_a_401_without_a_renewal_reaches_the_caller(sleeps):
    c = _client(_Response(401))
    c._renew_login = AsyncMock(return_value=False)
    with pytest.raises(aiohttp.ClientResponseError):
        await c._api_request("tracks/1")
    assert c.session.calls == 1


async def test_a_request_is_renewed_only_once(sleeps):
    c = _client(_Response(401), _Response(401))
    c._renew_login = AsyncMock(return_value=True)
    with pytest.raises(aiohttp.ClientResponseError):
        await c._api_request("tracks/1")
    assert c.session.calls == 2
    c._renew_login.assert_awaited_once()


async def test_renew_login_refreshes_at_most_once_a_minute(monkeypatch):
    """Requests in flight together all hit the expiry at once; they retry on
    the one refresh, so the refresh token rotates only once."""
    c = _client()
    refreshed = 0

    async def refresh():
        nonlocal refreshed
        refreshed += 1

    monkeypatch.setattr(c, "_refresh_access_token", refresh)
    assert await c._renew_login()
    assert await c._renew_login()  # a sibling, in the same minute
    assert refreshed == 1


async def test_a_refused_renewal_stops_the_run(monkeypatch):
    """A suspended account refuses the refresh: every track after it would
    ask again and be refused too."""
    c = _client()
    refreshed = 0

    async def refused():
        nonlocal refreshed
        refreshed += 1
        raise AuthenticationError("Tidal refused the token refresh: suspended")

    monkeypatch.setattr(c, "_refresh_access_token", refused)
    with pytest.raises(AccountHalt, match=r"login failed mid-run: .*suspended"):
        await c._renew_login()
    # A sibling that was waiting for the renewal does not ask again, and no
    # request after it is sent.
    with pytest.raises(AccountHalt):
        await c._renew_login()
    with pytest.raises(AccountHalt):
        await c._api_request("tracks/1")
    assert refreshed == 1
    assert c.session.calls == 0


async def test_other_client_errors_are_not_retried(sleeps):
    c = _client(_Response(401))
    # Without a renewal behind it, a 401 is just another client error.
    c._renew_login = AsyncMock(return_value=False)
    with pytest.raises(aiohttp.ClientResponseError):
        await c._api_request("tracks/1")
    assert c.session.calls == 1
    assert sleeps == []


async def test_ssl_errors_are_not_retried(sleeps):
    c = _client(aiohttp.ClientSSLError(MagicMock(), OSError("bad certificate")))
    with pytest.raises(aiohttp.ClientSSLError):
        await c._api_request("tracks/1")
    assert c.session.calls == 1
    assert sleeps == []
