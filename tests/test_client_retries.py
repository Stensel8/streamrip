"""Qobuz and SoundCloud API requests get the same retries Tidal's always had."""

import asyncio
import base64
import contextlib
from types import SimpleNamespace

import aiohttp
import pytest

from streamrip.client import client as client_module
from streamrip.client.qobuz import QobuzClient
from streamrip.client.soundcloud import SoundcloudClient
from streamrip.config import Config

TOKEN = "SECRET-TOKEN-abcdefghij"


class _Response:
    def __init__(self, status=200, body=None, content_type="application/json"):
        self.status = status
        self.headers = {}
        self.content_type = content_type
        self._body = body if body is not None else {"ok": True}

    async def text(self, encoding="utf-8"):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

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


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def _qobuz(*outcomes):
    c = QobuzClient(Config.defaults())
    c.rate_limiter = contextlib.nullcontext()
    c.session = _Session(*outcomes)
    return c


@pytest.mark.asyncio
async def test_qobuz_dropped_connection_is_retried():
    c = _qobuz(aiohttp.ServerDisconnectedError(), _Response(body={"id": 1}))
    assert await c._api_request("track/get", {}) == (200, {"id": 1})
    assert c.session.calls == 2


@pytest.mark.asyncio
async def test_qobuz_edge_502_page_is_retried():
    c = _qobuz(_Response(502, content_type="text/html"), _Response(body={"id": 1}))
    assert await c._api_request("album/get", {}) == (200, {"id": 1})
    assert c.session.calls == 2


@pytest.mark.asyncio
async def test_qobuz_client_errors_are_returned_not_retried():
    c = _qobuz(_Response(401, body={"message": "bad token"}))
    assert await c._api_request("user/login", {}) == (401, {"message": "bad token"})
    assert c.session.calls == 1


@pytest.mark.asyncio
async def test_qobuz_retry_warning_does_not_quote_the_token(caplog):
    c = _qobuz(aiohttp.ServerDisconnectedError(), _Response())
    await c._api_request("user/login", {"user_id": "1", "user_auth_token": TOKEN})
    assert "retrying" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_soundcloud_requests_go_through_its_rate_limiter_and_retry():
    c = SoundcloudClient(Config.defaults())
    c.rate_limiter = _CountingLimiter()
    c.session = _Session(_Response(503), _Response(body={"id": 7}))
    assert await c._request("https://api-v2.soundcloud.com/tracks/7") == (
        {"id": 7},
        200,
    )
    assert c.rate_limiter.entered == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_during", ["sleep", "admission"])
async def test_pause_extensions_require_fresh_admission(monkeypatch, pause_during):
    c = _qobuz(_Response(503), _Response(503), _Response(503), _Response())
    # Also exercise clients sharing another client's account pause.
    owner = _qobuz()
    owner._retry_at = 5.0
    monkeypatch.setattr(c, "_pause_owner", lambda: owner)
    now = 0.0
    extensions = 0
    events = []

    def extend_pause():
        nonlocal extensions
        if extensions < client_module.MAX_API_ATTEMPTS:
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
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(asyncio, "sleep", sleep)

    assert await c._api_request("track/get", {}) == (200, {"ok": True})
    assert c.session.calls == client_module.MAX_API_ATTEMPTS
    assert extensions == client_module.MAX_API_ATTEMPTS
    if pause_during == "admission":
        assert events[:13] == ["sleep", "admit", "exit"] * 4 + ["sleep"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["qobuz", "soundcloud"])
@pytest.mark.parametrize("failure_at", ["page", "bundle"])
@pytest.mark.parametrize("failure", [TimeoutError(), _Response(503)])
async def test_bootstrap_requests_retry_and_use_the_account_limiter(
    monkeypatch, provider, failure_at, failure
):
    """Both bootstrap fetches recover from transport and server failures."""
    if provider == "qobuz":
        client = _qobuz()
        page = '<script src="/resources/1.2.3-a123/bundle.js"></script>'
        bundle = 'production:{api:{appId:"123456789",appSecret:"' + "x" * 32 + '"'
        for timezone in ("paris", "london"):
            seed = base64.b64encode(timezone.encode()).decode() + "A" * 44
            bundle += f'x.initialSeed("{seed}",window.utimezone.{timezone})'
        expected = ("123456789", ["london", "paris"])
        fetch = client._get_app_id_and_secrets
    else:
        client = SoundcloudClient(Config.defaults())
        page = '<script src="/bundle.js"></script>'
        bundle = 'client_id:"' + "a" * 32 + '"'
        expected = ("a" * 32, "")
        fetch = client._refresh_tokens

    outcomes = [_Response(body=page), _Response(body=bundle)]
    outcomes.insert(0 if failure_at == "page" else 1, failure)
    session = _Session(*outcomes)
    client.rate_limiter = _CountingLimiter()
    if provider == "qobuz":
        monkeypatch.setattr("streamrip.client.qobuz.new_session", lambda **_: session)
    else:
        client.session = session

    assert await fetch() == expected
    assert session.calls == 3
    assert client.rate_limiter.entered == 3
    if provider == "qobuz":
        assert session.closed


@pytest.mark.asyncio
async def test_bootstrap_does_not_parse_a_persistent_http_error():
    """Exhausted text fetches raise the HTTP error instead of parsing its body."""
    c = _qobuz(*[_Response(503) for _ in range(client_module.MAX_API_ATTEMPTS)])
    with pytest.raises(aiohttp.ClientResponseError) as error:
        await c._get_text_with_retries("https://example.test/bundle.js")
    assert error.value.status == 503
    assert c.session.calls == client_module.MAX_API_ATTEMPTS
