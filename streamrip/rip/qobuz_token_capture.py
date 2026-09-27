"""Capture a Qobuz user id and user_auth_token from a real browser login.

Qobuz's web login is behind OAuth/reCAPTCHA, so streamrip cannot log in with a
password any more for most accounts. Instead, open a browser with Playwright,
let the user log in normally, and read the token from the ``user/login``
response. Playwright itself is a normal dependency; the Chromium build it
drives is fetched automatically, once, the first time it's actually needed
(``playwright install`` downloads a real browser, which can't be bundled in
the Python package).
"""

import asyncio
import importlib.util
import logging
import platform
import sys
import time

from ..console import console

logger = logging.getLogger("streamrip")

QOBUZ_LOGIN_PAGE = "https://play.qobuz.com/login"
QOBUZ_LOGIN_API = "https://www.qobuz.com/api.json/0.2/user/login"


class QobuzTokenCaptureError(Exception):
    """Raised when automatic Qobuz token capture fails."""


def playwright_available() -> bool:
    """Whether the optional Playwright dependency is installed."""
    return importlib.util.find_spec("playwright") is not None


async def _install_chromium() -> None:
    """Download the Chromium build Playwright drives, once.

    The Python package alone doesn't include a browser binary; Playwright
    fetches one on first use instead of streamrip shipping it, and this
    keeps that one-time fetch automatic instead of a documented manual step.
    """
    console.print(
        "[cyan]First-time setup: downloading a browser for automatic Qobuz "
        "login (about 150 MB, once only)..."
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise QobuzTokenCaptureError(
            "Could not download the browser automatically "
            f"(exit {proc.returncode}): {out.decode(errors='replace')[-500:]}"
        )


async def _capture_qobuz_auth_token_async(timeout_s: int = 300) -> tuple[str, str]:
    """Capture user id and auth token from Qobuz web login traffic.

    Returns:
        Tuple of (user_id, user_auth_token)
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - import path only
        raise QobuzTokenCaptureError(
            "Automatic browser capture requires Playwright, which failed to "
            "import. Reinstall streamrip, or enter the token manually."
        ) from exc

    result: dict[str, str] = {}

    async def handle_response(response):
        if response.url != QOBUZ_LOGIN_API:
            return

        try:
            post_data = response.request.post_data or ""
            if post_data and "extra=partner" not in post_data:
                return
            if response.status != 200:
                return
            payload = await response.json()
        except Exception:
            return

        if not isinstance(payload, dict):
            return

        user = payload.get("user", {})
        user_id = user.get("id")
        token = payload.get("user_auth_token")
        if user_id is None or not token:
            return

        result["user_id"] = str(user_id)
        result["token"] = str(token)

    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=False)
            except Exception as launch_exc:
                if "playwright install" not in str(launch_exc):
                    raise
                await _install_chromium()
                browser = await playwright.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            page.on("response", handle_response)
            await page.goto(QOBUZ_LOGIN_PAGE, wait_until="domcontentloaded")

            logger.info(
                "Waiting for Qobuz login response in browser (timeout: %ss).",
                timeout_s,
            )
            deadline = time.monotonic() + timeout_s
            while "token" not in result and time.monotonic() < deadline:
                await page.wait_for_timeout(250)

            await context.close()
            await browser.close()
    except Exception as exc:
        raise QobuzTokenCaptureError(
            f"Automatic browser capture failed: {exc}"
        ) from exc

    if "token" not in result or "user_id" not in result:
        raise QobuzTokenCaptureError(
            "Could not detect a successful Qobuz user/login response. "
            "Please complete login in the opened browser or use manual token input."
        )

    return result["user_id"], result["token"]


def _capture_qobuz_auth_token_windows(timeout_s: int) -> tuple[str, str]:
    """Run Playwright capture in an isolated Proactor loop on Windows."""
    if not hasattr(asyncio, "ProactorEventLoop"):
        raise QobuzTokenCaptureError(
            "Windows Proactor event loop is unavailable; use manual token input."
        )

    loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_capture_qobuz_auth_token_async(timeout_s))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def capture_qobuz_auth_token(timeout_s: int = 300) -> tuple[str, str]:
    """Capture user id and auth token from Qobuz web login traffic."""
    if platform.system() == "Windows":
        return await asyncio.to_thread(_capture_qobuz_auth_token_windows, timeout_s)
    return await _capture_qobuz_auth_token_async(timeout_s)
