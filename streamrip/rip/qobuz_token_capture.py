"""Capture a Qobuz user id and user_auth_token from a browser.

Qobuz's web login has no server-side device-code flow to poll (unlike
Tidal's). It does, however, save a working `user_auth_token` for itself in
`localStorage["localuser"].token` the moment you're logged in -- and keeps
it there for as long as the session lasts, verified 2026-09-30 to be the
same token the api.json/0.2 login endpoint hands out. Two ways to read
that, both ending in the same (user_id, token) pair:

- `capture_qobuz_auth_token`: a short script, pasted into the browser you
  already have open *after* logging in, posts it to a short-lived
  localhost-only HTTP server started here. Nothing is downloaded or
  driven. Must be pasted after login, not before: Qobuz reloads the page
  on login, which would tear the script down before it could catch
  anything (verified 2026-09-30).
- `capture_qobuz_auth_token_via_browser`: Playwright drives a fresh,
  throwaway browser profile -- no console, no existing session to work
  around. Prefers a Chrome-family browser already on the machine; asks
  before downloading its own if none is found.
"""

import asyncio
import json
import logging
import secrets
import shutil
import sys

from aiohttp import web

from ..console import console
from .interactive import Confirm

logger = logging.getLogger("streamrip")

# Playwright's own channel names for already-installed browsers, tried
# first; then common binary names on PATH for browsers Playwright doesn't
# recognize as a channel (Brave, Vivaldi, plain Chromium builds, ...).
# Either way nothing is ever downloaded -- if none of these are found, the
# caller should fall back to the console-snippet or manual capture instead.
_CHROME_CHANNELS = ("chrome", "msedge")
_CHROME_BINARY_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "brave",
    "brave-origin",
    "microsoft-edge",
    "microsoft-edge-stable",
    "vivaldi",
    "vivaldi-stable",
)

# Qobuz's login page sets no restrictive Content-Security-Policy (checked
# 2026-09-30: `default-src * 'unsafe-inline' 'unsafe-eval' data: blob:`), so
# a fetch() from it to a 127.0.0.1 port is not blocked. Browsers also treat
# loopback addresses as a secure context, so an HTTPS page may call an
# http://127.0.0.1 URL without tripping mixed-content blocking. If Qobuz
# ever tightens that policy, the snippet simply stops reaching us and the
# caller falls back to manual entry -- nothing here depends on it silently.
QOBUZ_ORIGIN = "https://play.qobuz.com"

_SNIPPET_TEMPLATE = """\
(() => {{
  const target = "{callback_url}";
  const tryRead = () => {{
    let data;
    try {{ data = JSON.parse(localStorage.getItem("localuser")); }} catch (e) {{ return false; }}
    if (!data || !data.id || !data.token) return false;
    fetch(target, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{user_id: data.id, token: data.token}}),
    }}).then(() => console.log("streamrip: token sent ✅"))
      .catch((e) => console.error("streamrip: could not reach streamrip", e));
    return true;
  }};
  if (tryRead()) return;
  console.log("streamrip: not logged in yet. Log in, then paste this again --");
  console.log("streamrip: Qobuz reloads the page on login, which stops this script.");
  const id = setInterval(() => {{ if (tryRead()) clearInterval(id); }}, 1000);
}})();\
"""


class QobuzTokenCaptureError(Exception):
    """Raised when automatic Qobuz token capture fails or times out."""


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": QOBUZ_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


async def capture_qobuz_auth_token(timeout_s: int = 300) -> tuple[str, str]:
    """Print a console snippet, then wait for it to post back a token.

    Starts a 127.0.0.1-only HTTP server behind a random, single-use path so
    nothing else on the machine can feed it a bogus credential, prints the
    snippet to paste into the browser's console, and waits up to
    `timeout_s` seconds for that snippet to report the id and token it read
    out of Qobuz's own `localStorage`. Needs to be pasted *after* logging
    in: Qobuz reloads the page on login, which tears down any script
    pasted into the console beforehand, so it can't reliably catch a login
    that happens after the paste.

    Raises QobuzTokenCaptureError if nothing arrives in time or the server
    can't be started; callers should fall back to manual entry.
    """
    nonce = secrets.token_urlsafe(16)
    loop = asyncio.get_event_loop()
    result: "asyncio.Future[tuple[str, str]]" = loop.create_future()

    async def handle_preflight(request: web.Request) -> web.Response:
        return web.Response(status=204, headers=_cors_headers())

    async def handle_callback(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            user_id = str(payload["user_id"])
            token = str(payload["token"])
        except Exception:
            return web.Response(status=400, headers=_cors_headers())
        if not result.done():
            result.set_result((user_id, token))
        return web.Response(status=204, headers=_cors_headers())

    app = web.Application()
    path = f"/callback/{nonce}"
    app.router.add_route("OPTIONS", path, handle_preflight)
    app.router.add_post(path, handle_callback)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        callback_url = f"http://127.0.0.1:{site.port}{path}"

        console.print(
            "\n[cyan]Trying to capture the Qobuz login token automatically.[/cyan]\n"
            "[bold]Log in first[/bold] in the browser tab that just opened --\n"
            "Qobuz reloads the page on login, which would kill this script\n"
            "before it could catch anything. Once you're logged in, open the\n"
            "console (F12) and paste this:\n"
        )
        console.print(_SNIPPET_TEMPLATE.format(callback_url=callback_url))

        try:
            return await asyncio.wait_for(result, timeout=timeout_s)
        except asyncio.TimeoutError as e:
            raise QobuzTokenCaptureError(
                f"No token arrived within {timeout_s}s; enter it manually instead."
            ) from e
    finally:
        await runner.cleanup()


async def _launch_installed_chromium(pw, headless: bool):
    """Try to drive a browser already on this machine. Never downloads.

    Returns (browser, name) or (None, None) if nothing usable was found.
    """
    for channel in _CHROME_CHANNELS:
        try:
            browser = await pw.chromium.launch(channel=channel, headless=headless)
            return browser, channel
        except Exception:
            continue
    for name in _CHROME_BINARY_NAMES:
        path = shutil.which(name)
        if not path:
            continue
        try:
            browser = await pw.chromium.launch(executable_path=path, headless=headless)
            return browser, name
        except Exception:
            continue
    return None, None


async def _download_playwright_chromium() -> None:
    """Run `playwright install chromium`. Only called after the user agrees."""
    console.print(
        "[cyan]Downloading a browser for Qobuz auto-login (one-time, ~150 MB)...[/cyan]"
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
            "Could not download a browser automatically "
            f"(exit {proc.returncode}): {out.decode(errors='replace')[-500:]}"
        )


async def capture_qobuz_auth_token_via_browser(
    timeout_s: int = 120,
    login_url: str = "https://play.qobuz.com/login",
    headless: bool = False,
) -> tuple[str, str]:
    """Log in inside a freshly isolated browser window and read the token.

    Prefers driving a Chrome-family browser already on this machine (Chrome,
    Edge, Brave, Chromium, Vivaldi, ...) in a throwaway profile, so the
    login page is always fresh -- there's no existing session to work
    around, unlike `capture_qobuz_auth_token`'s console snippet. Playwright
    can't drive an already-installed Firefox or LibreWolf the same way (it
    needs its own specially patched build for that engine), so if no
    Chrome-family browser is found, this asks before downloading one --
    that download is Playwright's own browser, not the one you use day to
    day, and it's worth being upfront about that.

    `login_url` and `headless` exist for tests; real callers should leave
    them at their defaults (the real Qobuz login page, and a visible
    window, since this drives your actual login).

    Raises QobuzTokenCaptureError if no browser could be used (nothing
    found and the download was declined or failed) or no login happened
    within `timeout_s`; callers should fall back to another login method.
    """
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise QobuzTokenCaptureError(
            "Playwright isn't available; use another login method."
        ) from e

    async with async_playwright() as pw:
        browser, found_as = await _launch_installed_chromium(pw, headless)
        if browser is not None:
            console.print(f"[cyan]Driving your installed browser ({found_as})…[/cyan]")
        else:
            if not Confirm.ask(
                "\n[yellow]No installed Chrome, Edge, Brave, or Chromium "
                "found.[/yellow] Playwright would need to download its own "
                "browser (not the one you use day to day) to log in "
                "automatically -- about 150 MB, once. Download it?",
                default=False,
            ):
                raise QobuzTokenCaptureError(
                    "Skipped downloading a browser; use another login method."
                )
            await _download_playwright_chromium()
            browser = await pw.chromium.launch(headless=headless)
            console.print("[cyan]Using the downloaded browser…[/cyan]")

        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(login_url)

            deadline = asyncio.get_event_loop().time() + timeout_s
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await page.evaluate("() => localStorage.getItem('localuser')")
                except PlaywrightError:
                    # Qobuz reloads the page on login, which tears down the
                    # execution context mid-poll; the next tick runs against
                    # the page that comes after, once it's settled.
                    await asyncio.sleep(1)
                    continue
                if raw:
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        data = None
                    if data and data.get("id") and data.get("token"):
                        return str(data["id"]), str(data["token"])
                await asyncio.sleep(1)

            raise QobuzTokenCaptureError(f"No login detected within {timeout_s}s.")
        finally:
            await browser.close()
