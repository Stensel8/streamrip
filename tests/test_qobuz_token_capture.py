import asyncio
import re

import aiohttp
import pytest

from streamrip.rip.qobuz_token_capture import (
    QobuzTokenCaptureError,
    capture_qobuz_auth_token,
)

CALLBACK_RE = re.compile(r"http://127\.0\.0\.1:\d+/callback/[\w-]+")


def _mock_console_print(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(
        "streamrip.rip.qobuz_token_capture.console.print",
        lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
    )
    return printed


async def _wait_for_callback_url(printed: list[str]) -> str:
    for _ in range(100):
        match = CALLBACK_RE.search("\n".join(printed))
        if match:
            return match.group(0)
        await asyncio.sleep(0.02)
    raise AssertionError("capture never printed a callback URL")


async def test_capture_receives_the_token_the_snippet_posts(monkeypatch):
    printed = _mock_console_print(monkeypatch)
    task = asyncio.ensure_future(capture_qobuz_auth_token(timeout_s=5))
    callback_url = await _wait_for_callback_url(printed)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            callback_url,
            json={"user_id": "123", "token": "captured-token"},
            headers={"Origin": "https://play.qobuz.com"},
        ) as resp:
            assert resp.status == 204
            assert (
                resp.headers["Access-Control-Allow-Origin"] == "https://play.qobuz.com"
            )

    assert await task == ("123", "captured-token")


async def test_capture_times_out_when_nothing_is_posted(monkeypatch):
    _mock_console_print(monkeypatch)
    with pytest.raises(QobuzTokenCaptureError):
        await capture_qobuz_auth_token(timeout_s=0.2)


async def test_wrong_path_is_rejected_and_capture_still_times_out(monkeypatch):
    printed = _mock_console_print(monkeypatch)
    task = asyncio.ensure_future(capture_qobuz_auth_token(timeout_s=0.5))
    callback_url = await _wait_for_callback_url(printed)
    wrong_url = callback_url.rsplit("/", 1)[0] + "/wrong-nonce"

    async with aiohttp.ClientSession() as session:
        async with session.post(wrong_url, json={"user_id": "x", "token": "y"}) as resp:
            assert resp.status == 404

    with pytest.raises(QobuzTokenCaptureError):
        await task


async def test_malformed_payload_is_rejected(monkeypatch):
    printed = _mock_console_print(monkeypatch)
    task = asyncio.ensure_future(capture_qobuz_auth_token(timeout_s=0.5))
    callback_url = await _wait_for_callback_url(printed)

    async with aiohttp.ClientSession() as session:
        async with session.post(callback_url, json={"nonsense": True}) as resp:
            assert resp.status == 400

    with pytest.raises(QobuzTokenCaptureError):
        await task
