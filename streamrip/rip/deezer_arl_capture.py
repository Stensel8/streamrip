"""Capture the `arl` cookie of a Deezer web session you log in to.

Deezer has no sign-in for outside apps: what streamrip logs in with is the
`arl` cookie of a logged-in web session, and an email/password login is
behind a captcha. The cookie is HttpOnly, so unlike Qobuz's token it can't be
read by a script on the page -- only by the browser's owner. Playwright
drives a throwaway browser profile (see browser_login) and reads that one
cookie once you've logged in yourself; streamrip never sees your password and
leaves every other cookie of the session alone.
"""

import asyncio
import logging

from ..console import console
from .browser_login import BrowserLoginError, open_login_browser

logger = logging.getLogger("streamrip")

DEEZER_LOGIN_URL = "https://www.deezer.com/login"


class DeezerArlCaptureError(BrowserLoginError):
    """Raised when automatic Deezer ARL capture fails or times out."""


async def capture_deezer_arl_via_browser(
    timeout_s: int = 300,
    login_url: str = DEEZER_LOGIN_URL,
    headless: bool = False,
) -> str:
    """Log in inside a freshly isolated browser window and read the ARL.

    `login_url` and `headless` exist for tests; real callers should leave
    them at their defaults (the real Deezer login page, and a visible
    window, since this drives your actual login). The default timeout is
    generous: a login can include a captcha or a second factor.

    Raises DeezerArlCaptureError if no browser could be used or no login
    happened within `timeout_s`; callers should fall back to entering the
    ARL by hand.
    """
    try:
        async with open_login_browser("Deezer", headless) as browser:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(login_url)
            console.print(
                "\n[cyan]Log in to Deezer in the browser window that just opened."
                "[/cyan]\nstreamrip never sees your password: it only reads the "
                "[bold]arl[/bold] cookie once you're logged in.\n"
            )

            deadline = asyncio.get_event_loop().time() + timeout_s
            while asyncio.get_event_loop().time() < deadline:
                for cookie in await context.cookies(login_url):
                    if cookie["name"] == "arl" and cookie["value"]:
                        return cookie["value"]
                await asyncio.sleep(1)

            raise DeezerArlCaptureError(f"No login detected within {timeout_s}s.")
    except BrowserLoginError:
        raise
    except Exception as exc:
        raise DeezerArlCaptureError(f"Browser login failed: {exc}") from exc
