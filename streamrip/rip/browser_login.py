"""A throwaway browser window to log in to a service with.

Qobuz and Deezer have no login streamrip can do for you (Tidal's device code
aside), but both leave a credential in a logged-in web session. Playwright
drives a fresh browser profile -- no existing session to work around -- and
the caller reads that credential once you've logged in yourself. Prefers a
Chrome-family browser already on the machine; asks before downloading its
own if there is none.
"""

import asyncio
import logging
import shutil
import sys
from contextlib import asynccontextmanager

from ..console import console
from .interactive import Confirm

logger = logging.getLogger("streamrip")

# Playwright's own channel names for already-installed browsers, tried
# first; then common binary names on PATH for browsers Playwright doesn't
# recognize as a channel (Brave, Vivaldi, plain Chromium builds, ...).
# Either way nothing is ever downloaded -- if none of these are found, the
# caller should fall back to another login method.
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


class BrowserLoginError(Exception):
    """Raised when logging in through a browser window fails or times out."""


async def launch_installed_chromium(pw, headless: bool):
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


async def download_playwright_chromium(service: str) -> None:
    """Run `playwright install chromium`. Only called after the user agrees."""
    console.print(
        f"[cyan]Downloading a browser for {service} auto-login "
        "(one-time, ~150 MB)...[/cyan]"
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
        raise BrowserLoginError(
            "Could not download a browser automatically "
            f"(exit {proc.returncode}): {out.decode(errors='replace')[-500:]}"
        )


@asynccontextmanager
async def open_login_browser(service: str, headless: bool = False):
    """Yield a browser to log in to `service` with, closed again afterwards.

    Prefers driving a Chrome-family browser already on this machine (Chrome,
    Edge, Brave, Chromium, Vivaldi, ...). Playwright can't drive an
    already-installed Firefox or LibreWolf the same way (it needs its own
    specially patched build for that engine), so if no Chrome-family
    browser is found, this asks before downloading one -- that download is
    Playwright's own browser, not the one you use day to day, and it's worth
    being upfront about that.

    `headless` exists for tests; real callers should leave it off, since
    this drives your actual login.

    Raises BrowserLoginError if no browser could be used (nothing found and
    the download was declined or failed).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise BrowserLoginError(
            "Playwright isn't available; use another login method."
        ) from e

    async with async_playwright() as pw:
        browser, found_as = await launch_installed_chromium(pw, headless)
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
                raise BrowserLoginError(
                    "Skipped downloading a browser; use another login method."
                )
            await download_playwright_chromium(service)
            browser = await pw.chromium.launch(headless=headless)
            console.print("[cyan]Using the downloaded browser…[/cyan]")
        try:
            yield browser
        finally:
            await browser.close()
