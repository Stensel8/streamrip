import pytest
from aiohttp import web

from streamrip.rip.qobuz_token_capture import (
    QobuzTokenCaptureError,
    capture_qobuz_auth_token_via_browser,
)

pytestmark = pytest.mark.asyncio


async def _serve_page(html: str) -> tuple[web.AppRunner, str]:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=html, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site.port}/"


async def test_reads_token_already_in_local_storage():
    html = """
    <script>
      localStorage.setItem("localuser", JSON.stringify({id: 123, token: "abc-token"}));
    </script>
    """
    runner, url = await _serve_page(html)
    try:
        result = await capture_qobuz_auth_token_via_browser(
            timeout_s=15, login_url=url, headless=True
        )
    finally:
        await runner.cleanup()

    assert result == ("123", "abc-token")


async def test_catches_token_that_appears_after_a_delay():
    html = """
    <script>
      setTimeout(() => {
        localStorage.setItem("localuser", JSON.stringify({id: 456, token: "delayed-token"}));
      }, 1500);
    </script>
    """
    runner, url = await _serve_page(html)
    try:
        result = await capture_qobuz_auth_token_via_browser(
            timeout_s=15, login_url=url, headless=True
        )
    finally:
        await runner.cleanup()

    assert result == ("456", "delayed-token")


async def test_times_out_when_never_logged_in():
    runner, url = await _serve_page("<p>never logs in</p>")
    try:
        with pytest.raises(QobuzTokenCaptureError):
            await capture_qobuz_auth_token_via_browser(
                timeout_s=2, login_url=url, headless=True
            )
    finally:
        await runner.cleanup()


async def _serve_two_pages(first_html: str, second_html: str) -> tuple[web.AppRunner, str]:
    async def first(request: web.Request) -> web.Response:
        return web.Response(text=first_html, content_type="text/html")

    async def second(request: web.Request) -> web.Response:
        return web.Response(text=second_html, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", first)
    app.router.add_get("/after", second)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site.port}/"


async def test_survives_a_page_navigation_during_login():
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
    runner, url = await _serve_two_pages(first_html, second_html)
    try:
        result = await capture_qobuz_auth_token_via_browser(
            timeout_s=15, login_url=url, headless=True
        )
    finally:
        await runner.cleanup()

    assert result == ("999", "post-nav-token")


async def test_ignores_incomplete_local_storage_entry():
    html = """
    <script>
      localStorage.setItem("localuser", JSON.stringify({id: 789}));
    </script>
    """
    runner, url = await _serve_page(html)
    try:
        with pytest.raises(QobuzTokenCaptureError):
            await capture_qobuz_auth_token_via_browser(
                timeout_s=2, login_url=url, headless=True
            )
    finally:
        await runner.cleanup()
