"""Qobuz and SoundCloud API requests get the same retries Tidal's always had."""

import asyncio
import contextlib
from types import SimpleNamespace

import aiohttp
import pytest

import streamrip.client.client as client_module
from streamrip.client.client import MAX_API_ATTEMPTS
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
async def test_pause_extended_during_sleep_is_checked_before_admission(monkeypatch):
    c = _qobuz(_Response())
    c.rate_limiter = _CountingLimiter()
    now = 100.0
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(client_module.random, "random", lambda: 0)
    c._pause_requests(5)
    waits = []

    async def sleep(delay):
        nonlocal now
        assert c.rate_limiter.entered == 0
        assert c.session.calls == 0
        waits.append(delay)
        now += delay
        if len(waits) == 1:
            c._pause_requests(7)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert await c._api_request("track/get", {}) == (200, {"ok": True})
    assert waits == [5, 7]
    assert c.rate_limiter.entered == 1


@pytest.mark.asyncio
async def test_pause_after_admission_requires_fresh_capacity_without_using_attempts(
    monkeypatch,
):
    c = _qobuz(
        *[aiohttp.ServerDisconnectedError() for _ in range(MAX_API_ATTEMPTS - 1)],
        _Response(),
    )
    now = 100.0
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: now))

    class PausingLimiter(_CountingLimiter):
        active = False

        async def __aenter__(self):
            await super().__aenter__()
            self.active = True
            # Model another request receiving 429 while this one queues.
            if self.entered <= MAX_API_ATTEMPTS:
                assert c.session.calls == 0
                c._pause_requests(5)

        async def __aexit__(self, *exc):
            self.active = False

    c.rate_limiter = PausingLimiter()

    async def sleep(delay):
        nonlocal now
        assert not c.rate_limiter.active
        now += delay

    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert await c._api_request("track/get", {}) == (200, {"ok": True})
    assert c.session.calls == MAX_API_ATTEMPTS
    assert c.rate_limiter.entered == 2 * MAX_API_ATTEMPTS
