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


def _page(html="<p>login</p>", cookies=None):
    """A handler that serves html and sets the given HttpOnly cookies."""

    async def handler(request: web.Request) -> web.Response:
        """Answer with the page, setting the cookies as HttpOnly."""
        response = web.Response(text=html, content_type="text/html")
        for name, value in (cookies or {}).items():
            response.set_cookie(name, value, httponly=True)
        return response

    return handler


async def test_reads_the_httponly_arl_cookie(serve):
    """The arl is read although it is HttpOnly."""
    url = await serve({"/": _page(cookies={"arl": "the-arl"})})
    arl = await capture_deezer_arl_via_browser(
        timeout_s=15, login_url=url, headless=True
    )

    assert arl == "the-arl"


async def test_catches_an_arl_that_appears_after_a_delay(serve):
    """An arl set a moment after the page loads is still caught."""
    html = "<script>setTimeout(() => fetch('/logged-in'), 1500)</script>"
    url = await serve(
        {"/": _page(html), "/logged-in": _page(cookies={"arl": "delayed-arl"})}
    )
    arl = await capture_deezer_arl_via_browser(
        timeout_s=15, login_url=url, headless=True
    )

    assert arl == "delayed-arl"


async def test_other_cookies_of_the_session_are_not_enough(serve):
    """Only the arl counts as a login.

    A logged-in session carries a jwt, a refresh token and payment details too, and none
    of those are wanted.
    """
    url = await serve(
        {"/": _page(cookies={"sid": "s", "jwt": "j", "refresh-token": "r"})}
    )
    with pytest.raises(DeezerArlCaptureError, match="No login detected"):
        await capture_deezer_arl_via_browser(timeout_s=2, login_url=url, headless=True)


async def test_times_out_when_never_logged_in(serve):
    """Without a login the capture gives up with its own error."""
    url = await serve({"/": _page()})
    with pytest.raises(DeezerArlCaptureError):
        await capture_deezer_arl_via_browser(timeout_s=2, login_url=url, headless=True)


def _failing_browser(error: Exception):
    """An open_login_browser that raises `error` instead of opening a browser."""

    @asynccontextmanager
    async def open_login_browser(service, headless=False):
        """Raise before there is a browser."""
        raise error
        yield  # pragma: no cover

    return open_login_browser


async def test_a_generic_browser_error_comes_out_as_its_own_for_the_fallback(
    monkeypatch,
):
    """A plain BrowserLoginError comes out as DeezerArlCaptureError.

    That is the only error the prompter falls back to manual entry on.
    """
    error = BrowserLoginError("Skipped downloading a browser")
    monkeypatch.setattr(browser_login, "open_login_browser", _failing_browser(error))

    with pytest.raises(DeezerArlCaptureError, match="Skipped downloading") as raised:
        await capture_deezer_arl_via_browser()

    assert raised.value.__cause__ is error


async def test_declining_the_browser_download_allows_the_manual_fallback(monkeypatch):
    """With no installed browser and the 150 MB download declined, the capture fails in
    a way the prompter can fall back from.
    """
    monkeypatch.setattr(
        browser_login, "launch_installed_chromium", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(browser_login.Confirm, "ask", MagicMock(return_value=False))

    with pytest.raises(DeezerArlCaptureError, match="Skipped downloading"):
        await capture_deezer_arl_via_browser()


async def test_any_other_failure_is_wrapped_for_the_fallback(monkeypatch):
    """Any other failure is wrapped in DeezerArlCaptureError, keeping the original as
    its cause.
    """
    error = RuntimeError("the window was closed")
    monkeypatch.setattr(browser_login, "open_login_browser", _failing_browser(error))

    with pytest.raises(DeezerArlCaptureError, match="window was closed") as raised:
        await capture_deezer_arl_via_browser()

    assert raised.value.__cause__ is error
