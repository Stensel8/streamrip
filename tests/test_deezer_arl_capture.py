from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from streamrip.rip import browser_login
from streamrip.rip.browser_login import BrowserLoginError
from streamrip.rip.deezer_arl_capture import (
    DeezerArlCaptureError,
    capture_deezer_arl_via_browser,
)

pytestmark = pytest.mark.asyncio


async def _serve(handlers: dict) -> tuple[web.AppRunner, str]:
    app = web.Application()
    for path, handler in handlers.items():
        app.router.add_get(path, handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site.port}/"


def _page(html="<p>login</p>", cookies=None):
    """A handler that serves html and sets the given HttpOnly cookies."""

    async def handler(request: web.Request) -> web.Response:
        response = web.Response(text=html, content_type="text/html")
        for name, value in (cookies or {}).items():
            response.set_cookie(name, value, httponly=True)
        return response

    return handler


async def test_reads_the_httponly_arl_cookie():
    runner, url = await _serve({"/": _page(cookies={"arl": "the-arl"})})
    try:
        arl = await capture_deezer_arl_via_browser(
            timeout_s=15, login_url=url, headless=True
        )
    finally:
        await runner.cleanup()

    assert arl == "the-arl"


async def test_catches_an_arl_that_appears_after_a_delay():
    html = "<script>setTimeout(() => fetch('/logged-in'), 1500)</script>"
    runner, url = await _serve(
        {"/": _page(html), "/logged-in": _page(cookies={"arl": "delayed-arl"})}
    )
    try:
        arl = await capture_deezer_arl_via_browser(
            timeout_s=15, login_url=url, headless=True
        )
    finally:
        await runner.cleanup()

    assert arl == "delayed-arl"


async def test_other_cookies_of_the_session_are_not_enough():
    # Logged-in sessions carry a jwt, a refresh token and payment details too;
    # only the arl is ever wanted, and nothing else counts as a login.
    runner, url = await _serve(
        {"/": _page(cookies={"sid": "s", "jwt": "j", "refresh-token": "r"})}
    )
    try:
        with pytest.raises(DeezerArlCaptureError, match="No login detected"):
            await capture_deezer_arl_via_browser(
                timeout_s=2, login_url=url, headless=True
            )
    finally:
        await runner.cleanup()


async def test_times_out_when_never_logged_in():
    runner, url = await _serve({"/": _page()})
    try:
        with pytest.raises(DeezerArlCaptureError):
            await capture_deezer_arl_via_browser(
                timeout_s=2, login_url=url, headless=True
            )
    finally:
        await runner.cleanup()


def _failing_browser(error: Exception):
    @asynccontextmanager
    async def open_login_browser(service, headless=False):
        raise error
        yield  # pragma: no cover

    return open_login_browser


async def test_a_generic_browser_error_comes_out_as_its_own_for_the_fallback(
    monkeypatch,
):
    # The prompter falls back to manual entry on DeezerArlCaptureError only.
    error = BrowserLoginError("Skipped downloading a browser")
    monkeypatch.setattr(browser_login, "open_login_browser", _failing_browser(error))

    with pytest.raises(DeezerArlCaptureError, match="Skipped downloading") as raised:
        await capture_deezer_arl_via_browser()

    assert raised.value.__cause__ is error


async def test_declining_the_browser_download_allows_the_manual_fallback(monkeypatch):
    # No installed browser, and the answer to the 150 MB download is no.
    monkeypatch.setattr(
        browser_login, "launch_installed_chromium", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(browser_login.Confirm, "ask", MagicMock(return_value=False))

    with pytest.raises(DeezerArlCaptureError, match="Skipped downloading"):
        await capture_deezer_arl_via_browser()


async def test_any_other_failure_is_wrapped_for_the_fallback(monkeypatch):
    error = RuntimeError("the window was closed")
    monkeypatch.setattr(browser_login, "open_login_browser", _failing_browser(error))

    with pytest.raises(DeezerArlCaptureError, match="window was closed") as raised:
        await capture_deezer_arl_via_browser()

    assert raised.value.__cause__ is error
