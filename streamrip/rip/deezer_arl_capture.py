"""Capture the `arl` cookie of a Deezer web session you log in to.

Deezer has no sign-in for outside apps: what streamrip logs in with is the
`arl` cookie of a logged-in web session, and an email/password login is
behind a captcha. The cookie is HttpOnly, so unlike Qobuz's token it can't be
read by a script on the page -- only by the browser's owner. Playwright
drives a throwaway browser profile (see browser_login) and reads that one
cookie once you've logged in yourself; streamrip never sees your password and
leaves every other cookie of the session alone.
"""

from .browser_login import (
    LOGIN_TIMEOUT_S,
    BrowserLoginError,
    login_page,
    wait_for_login,
)

DEEZER_LOGIN_URL = "https://www.deezer.com/login"


class DeezerArlCaptureError(BrowserLoginError):
    """Raised when automatic Deezer ARL capture fails or times out."""


async def _read_arl(page, url: str) -> str | None:
    """The arl cookie once Deezer has set it, else None."""
    for cookie in await page.context.cookies(url):
        if cookie["name"] == "arl" and cookie["value"]:
            return cookie["value"]
    return None


async def capture_deezer_arl_via_browser(
    timeout_s: int = LOGIN_TIMEOUT_S,
    login_url: str = DEEZER_LOGIN_URL,
    headless: bool = False,
) -> str:
    """Log in inside a freshly isolated browser window and read the ARL.

    `login_url` and `headless` exist for tests; real callers should leave
    them at their defaults (the real Deezer login page, and a visible
    window, since this drives your actual login).

    Raises DeezerArlCaptureError if no browser could be used or no login
    happened within `timeout_s`; callers should fall back to entering the
    ARL by hand.
    """
    async with login_page(
        "Deezer", login_url, DeezerArlCaptureError, "the arl cookie", headless
    ) as page:
        return await wait_for_login(
            lambda: _read_arl(page, login_url), timeout_s, DeezerArlCaptureError
        )
