"""Capture the `arl` cookie of a Deezer web session you log in to.

Deezer has no sign-in for outside apps: what streamrip logs in with is the
`arl` cookie of a logged-in web session, and an email/password login is
behind a captcha. The cookie is HttpOnly, but the site hands it to a script
of its own page that asks (`user.getArl`): `deezer_navigate_snippet` is that
script, for the web interface. Here, Playwright drives a throwaway browser
profile (see browser_login) and, once you've logged in yourself, hands over
the cookies of the page; streamrip keeps the arl and discards the rest unread.
It never sees your password.
"""

from .browser_login import (
    LOGIN_TIMEOUT_S,
    BrowserLoginError,
    login_page,
    wait_for_login,
)

DEEZER_LOGIN_URL = "https://www.deezer.com/login"

# Run on www.deezer.com once logged in: it asks the site for the arl, and moves
# the tab to `callback_url` with it after the "#", which stays in the browser.
_NAVIGATE_TEMPLATE = """\
(async () => {{
  const gw = (method, token) => fetch(
    `/ajax/gw-light.php?method=${{method}}&input=3&api_version=1.0&api_token=${{token}}`,
    {{method: "POST", body: "{{}}"}},
  ).then((r) => r.json());
  const user = (await gw("deezer.getUserData", "null")).results;
  if (!user?.USER?.USER_ID) return console.log("streamrip: log in to Deezer first, then run this again");
  const arl = (await gw("user.getArl", user.checkForm)).results;
  location.href = "{callback_url}#" + encodeURIComponent(JSON.stringify({{arl, challenge: "{challenge}"}}));
}})();\
"""


def deezer_navigate_snippet(callback_url: str, challenge: str) -> str:
    """The script that hands the arl over by moving the tab to `callback_url`,
    with the `challenge` that says it is ours."""
    return _NAVIGATE_TEMPLATE.format(callback_url=callback_url, challenge=challenge)


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
