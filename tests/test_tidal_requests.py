"""Tidal API requests survive a flaky network and back off from rate limits."""

import asyncio
import contextlib
from unittest.mock import MagicMock

import aiohttp
import pytest

from streamrip.client.client import new_session
from streamrip.client.tidal import (
    MAX_API_ATTEMPTS,
    MAX_RETRY_DELAY,
    RATE_LIMIT_PAUSE,
    TidalClient,
)
from streamrip.config import Config
from streamrip.exceptions import ItemNotFoundError


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

    def get(self, url, params=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def sleeps(monkeypatch):
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def _client(*outcomes):
    c = TidalClient(Config.defaults())
    c.rate_limiter = contextlib.nullcontext()
    c.session = _Session(*outcomes)
    return c


@pytest.mark.asyncio
async def test_connection_error_is_retried(sleeps):
    c = _client(aiohttp.ServerDisconnectedError(), _Response(body={"id": 1}))
    assert await c._api_request("tracks/1") == {"id": 1}
    assert c.session.calls == 2
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_timeout_is_retried(sleeps):
    c = _client(TimeoutError(), _Response())
    assert await c._api_request("tracks/1") == {"ok": True}
    assert c.session.calls == 2


@pytest.mark.asyncio
async def test_server_error_is_retried(sleeps):
    c = _client(_Response(503), _Response(502), _Response(body={"id": 2}))
    assert await c._api_request("tracks/1") == {"id": 2}
    assert c.session.calls == 3


@pytest.mark.asyncio
async def test_gives_up_after_the_last_attempt_and_raises(sleeps):
    c = _client(*[aiohttp.ServerDisconnectedError() for _ in range(MAX_API_ATTEMPTS)])
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await c._api_request("tracks/1")
    assert c.session.calls == MAX_API_ATTEMPTS
    assert len(sleeps) == MAX_API_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_persistent_server_error_raises_the_http_error(sleeps):
    c = _client(*[_Response(503) for _ in range(MAX_API_ATTEMPTS)])
    with pytest.raises(aiohttp.ClientResponseError) as e:
        await c._api_request("tracks/1")
    assert e.value.status == 503


@pytest.mark.asyncio
async def test_rate_limit_waits_for_retry_after_and_pauses_later_requests(sleeps):
    c = _client(_Response(429, headers={"Retry-After": "7"}), _Response(), _Response())
    await c._api_request("tracks/1")
    # This one never saw a 429 itself, but the account is still rate limited.
    await c._api_request("tracks/2")
    assert c.session.calls == 3
    # +0-2s jitter, so concurrent requests don't all wake and re-trip it together.
    assert all(7 <= s < 9 for s in sleeps)


@pytest.mark.asyncio
async def test_rate_limit_without_retry_after_uses_a_default_pause(sleeps):
    c = _client(_Response(429), _Response())
    await c._api_request("tracks/1")
    assert len(sleeps) == 1
    assert RATE_LIMIT_PAUSE <= sleeps[0] < RATE_LIMIT_PAUSE + 2


@pytest.mark.asyncio
async def test_absurd_retry_after_is_capped(sleeps):
    c = _client(_Response(429, headers={"Retry-After": "3600"}), _Response())
    await c._api_request("tracks/1")
    assert len(sleeps) == 1
    assert MAX_RETRY_DELAY <= sleeps[0] < MAX_RETRY_DELAY + 2


@pytest.mark.asyncio
async def test_not_found_is_not_retried(sleeps):
    c = _client(_Response(404))
    with pytest.raises(ItemNotFoundError):
        await c._api_request("tracks/1/lyrics")
    assert c.session.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_other_client_errors_are_not_retried(sleeps):
    c = _client(_Response(401))
    with pytest.raises(aiohttp.ClientResponseError):
        await c._api_request("tracks/1")
    assert c.session.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_ssl_errors_are_not_retried(sleeps):
    c = _client(aiohttp.ClientSSLError(MagicMock(), OSError("bad certificate")))
    with pytest.raises(aiohttp.ClientSSLError):
        await c._api_request("tracks/1")
    assert c.session.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_sessions_do_not_wait_five_minutes_on_a_stalled_connection():
    session = new_session()
    try:
        assert session.timeout.total is None
        assert session.timeout.sock_read == 30
    finally:
        await session.close()
