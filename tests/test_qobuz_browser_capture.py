import pytest
from aiohttp import web

from streamrip.rip.qobuz_token_capture import (
    QobuzTokenCaptureError,
    capture_qobuz_auth_token_via_browser,
)

pytestmark = pytest.mark.asyncio


def _page(html: str):
    """A handler that answers with `html`."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=html, content_type="text/html")

    return handler


async def test_reads_token_already_in_local_storage(serve):
    html = """
    <script>
      localStorage.setItem("localuser", JSON.stringify({id: 123, token: "abc-token"}));
    </script>
    """
    url = await serve({"/": _page(html)})
    result = await capture_qobuz_auth_token_via_browser(
        timeout_s=15, login_url=url, headless=True
    )

    assert result == ("123", "abc-token")


async def test_catches_token_that_appears_after_a_delay(serve):
    html = """
    <script>
      setTimeout(() => {
        localStorage.setItem("localuser", JSON.stringify({id: 456, token: "delayed-token"}));
      }, 1500);
    </script>
    """
    url = await serve({"/": _page(html)})
    result = await capture_qobuz_auth_token_via_browser(
        timeout_s=15, login_url=url, headless=True
    )

    assert result == ("456", "delayed-token")


async def test_times_out_when_never_logged_in(serve):
    url = await serve({"/": _page("<p>never logs in</p>")})
    with pytest.raises(QobuzTokenCaptureError):
        await capture_qobuz_auth_token_via_browser(
            timeout_s=2, login_url=url, headless=True
        )


async def test_survives_a_page_navigation_during_login(serve):
    """Qobuz reloads the page on login, which destroys the JS execution
    context mid-poll (observed live 2026-09-30: an uncaught Playwright
    "Execution context was destroyed" error used to crash the whole
    capture). This must instead just keep polling against whatever page
    comes next.
    """
    first_html = '<script>window.location.href = "/after";</script>'
    second_html = """
    <script>
      setTimeout(() => {
        localStorage.setItem("localuser", JSON.stringify({id: 999, token: "post-nav-token"}));
      }, 200);
    </script>
    """
    url = await serve({"/": _page(first_html), "/after": _page(second_html)})
    result = await capture_qobuz_auth_token_via_browser(
        timeout_s=15, login_url=url, headless=True
    )

    assert result == ("999", "post-nav-token")


async def test_ignores_incomplete_local_storage_entry(serve):
    html = """
    <script>
      localStorage.setItem("localuser", JSON.stringify({id: 789}));
    </script>
    """
    url = await serve({"/": _page(html)})
    with pytest.raises(QobuzTokenCaptureError):
        await capture_qobuz_auth_token_via_browser(
            timeout_s=2, login_url=url, headless=True
        )
