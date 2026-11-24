"""Logging in to Spotify, or to Tidal's official API, from the web interface.

Spotify has two ways: the pair link (approved on any device, no app of
one's own), and the browser login of the command line (rip/spotify_login.py)
with one's own app: the service sends the browser back to a port on this
computer, caught here. Where that cannot work (the browser is on another
device), the page takes the address the browser ended up on instead.
"""

import itertools
import secrets
import threading
import time

from .. import spotify_auth
from ..rip.spotify_login import (
    LOGIN_TIMEOUT,
    SpotifyLoginError,
    _wait_for_redirect,
    check_redirect,
    code_from_redirect_url,
)
from ..sync.auth import pkce_pair
lazy from ..config import Config
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
        # A Spotify login made in the browser is an app login; Tidal has no
        # notion of a method.
        method = "app" if self.oauth.name == "spotify" else ""
        self.store.save(self.oauth.name, token, method)
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


class DeviceLogin:
    """A pair link, approved on any device, no app of one's own. The token
    that comes is kept as a "device" login."""

    label = "Spotify"

    def __init__(self, config: Config):
        self.config = config
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    def run(self, job: Job) -> str:
        job.enter("login", "Asking Spotify for the pair link")
        try:
            pair = spotify_auth.request_device_code(self.config)
            job.link = pair.verification_uri_complete
            job.text = (
                f"Open the link on any device, and approve the login with the "
                f"code {pair.user_code}."
            )
            token = spotify_auth.poll_for_token(
                pair.device_code,
                self.config,
                expires_in=pair.expires_in,
                interval=pair.interval,
                stop=self._stop.is_set,
            )
        except spotify_auth.SpotifyAuthError:
            raise spotify_auth.SpotifyAuthError(
                "our automated oAuth2 failed. Use the 'Log in with your own "
                "app' button on this page."
            ) from None
        spotify_auth.save(self.config, token, "device")
        return f"Logged in to {self.label}."
