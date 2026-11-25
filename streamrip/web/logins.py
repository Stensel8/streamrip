"""Logging in to Spotify, or to Tidal's official API, from the web interface.

The browser login of the command line (rip/spotify_login.py), with one's own
app: the service sends the browser back to a port on this computer, caught
here. Where that cannot work (the browser is on another device), the page takes
the address the browser ended up on instead.
"""

import itertools
import secrets
import threading
import time

from ..exceptions import AuthenticationError
from ..rip.qobuz_token_capture import qobuz_navigate_snippet
from ..rip.spotify_login import (
    LOGIN_TIMEOUT,
    SpotifyLoginError,
    _wait_for_redirect,
    check_redirect,
    code_from_redirect_url,
)
from ..sync.auth import pkce_pair
lazy from ..sync.auth import ConfigTokenStore, OAuthClient
lazy from .jobs import Job

# Tries to take the login port, half a second apart.
BIND_ATTEMPTS = 6


class BrowserLogin:
    """One login: the link to send the browser to, and the code that comes back."""

    def __init__(self, oauth: OAuthClient, store: ConfigTokenStore):
        self.oauth, self.store = oauth, store
        self.verifier, challenge = pkce_pair()
        self.state = secrets.token_urlsafe(16)
        self.url = oauth.authorize_url(self.state, challenge)
        self._stop = threading.Event()
        self._pasted: str | None = None

    def paste(self, address: str):
        """The address the browser ended up on, given by hand."""
        self._pasted = code_from_redirect_url(address, self.state, self.oauth.label)
        self._stop.set()

    def run(self, job: Job) -> str:
        """Wait for the browser to come back, then trade the code for tokens."""
        label = self.oauth.label
        job.enter("login", f"Log in to {label} in the tab that opened.")
        job.link, job.pasteable = self.url, True
        params: dict[str, str] = {}
        try:
            params = self._wait()
        except SpotifyLoginError as e:
            if self._pasted is None and "Cannot listen" in str(e):
                # Something else has the port (a login on the command line):
                # the pasted address is the only way.
                job.text = f"{e} Paste the address you end up on below."
                self._stop.wait(LOGIN_TIMEOUT)
            if self._pasted is None:
                raise
        code = self._pasted or check_redirect(params, self.state, label)
        job.text = f"Finishing the {label} login"
        token = self.oauth.exchange(code, self.verifier)
        self.store.save(self.oauth.name, token)
        return f"Logged in to {label}."

    def _wait(self) -> dict[str, str]:
        """Listen for the browser. A login given up on a moment ago may still
        hold the port for a second: try again briefly before giving up."""
        for attempt in itertools.count(1):
            try:
                return _wait_for_redirect(
                    self.oauth.redirect_uri, LOGIN_TIMEOUT, self._stop
                )
            except SpotifyLoginError as e:
                if "Cannot listen" not in str(e) or attempt >= BIND_ATTEMPTS:
                    raise
            time.sleep(0.5)
        raise AssertionError  # itertools.count() never ends

    def cancel(self):
        self._stop.set()


QOBUZ_LOGIN_URL = "https://play.qobuz.com/login"


class QobuzScriptLogin:
    """Qobuz by a short script, run on play.qobuz.com once you are logged in
    there: it reads the user id and token the web player keeps, and posts them
    back. Only a post with this login's challenge is taken, once."""

    def __init__(self, callback_url: str):
        self.challenge = secrets.token_urlsafe(16)
        self.script = qobuz_navigate_snippet(callback_url, self.challenge)
        self._arrived = threading.Event()
        self._credentials: tuple[str, str] | None = None
        self._cancelled = False

    def receive(self, challenge: str, user_id: str, token: str) -> bool:
        """The script's post. True when it is this login's, and the first."""
        if not (user_id and token) or self._credentials is not None:
            return False
        if not secrets.compare_digest(challenge.encode(), self.challenge.encode()):
            return False
        self._credentials = (user_id, token)
        self._arrived.set()
        return True

    def run(self, job: Job, finish) -> str:
        """Wait for the script's post, then log in with it (`finish`)."""
        job.enter(
            "login",
            "Log in at Qobuz in the tab that opened. Then run the script below on that page.",
        )
        job.link, job.script = QOBUZ_LOGIN_URL, self.script
        self._arrived.wait(LOGIN_TIMEOUT)
        job.script = ""
        if self._cancelled:
            raise AuthenticationError("The Qobuz login was cancelled.")
        if self._credentials is None:
            raise AuthenticationError("No Qobuz token came back within 5 minutes.")
        job.text = "Checking the Qobuz login"
        finish(*self._credentials)
        return "Logged in to Qobuz."

    def cancel(self):
        self._cancelled = True
        self._arrived.set()
