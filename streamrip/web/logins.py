"""Logging in to Spotify, or to Tidal's official API, from the web interface;
to Qobuz on its own login page; and to Qobuz and Deezer by a script.

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
from ..rip.deezer_arl_capture import DEEZER_LOGIN_URL, deezer_navigate_snippet
from ..rip.qobuz_token_capture import qobuz_navigate_snippet, qobuz_oauth_code
from ..rip.spotify_login import (
    LOGIN_TIMEOUT,
    SpotifyLoginError,
    _wait_for_redirect,
    check_redirect,
    code_from_redirect_url,
    redirect_params,
)
from ..sync.auth import pkce_pair
lazy from ..sync.auth import ConfigTokenStore, OAuthClient
lazy from .jobs import Job
lazy from .runner import Runner

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
        job.offer(self.url, pasteable=True)
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


class QobuzLogin:
    """Qobuz's own login page, as its web player logs in: Qobuz sends the
    browser back here with a code. It sends no OAuth state back, so a random
    path stands in for it: only this login's address is taken, once."""

    def __init__(self, host_url: str):
        self.secret = secrets.token_urlsafe(16)
        self.redirect = f"{host_url}accounts/qobuz/oauth/{self.secret}"
        self._arrived = threading.Event()
        self._params: dict[str, str] | None = None
        self._cancelled = False

    def receive(self, secret: str, params: dict[str, str]) -> bool:
        """Qobuz's redirect. True when it is this login's, and the first."""
        if self._params is not None:
            return False
        if not secrets.compare_digest(secret.encode(), self.secret.encode()):
            return False
        self._params = params
        self._arrived.set()
        return True

    def paste(self, address: str):
        """The address the browser ended up on, given by hand."""
        self._params = redirect_params(address)
        self._arrived.set()

    def run(self, job: Job, runner: Runner) -> str:
        job.enter("login", "Getting Qobuz's login page")
        url, token = runner.qobuz_oauth(self.redirect)
        job.text = "Open Qobuz's login page and log in there."
        job.offer(url, pasteable=True)
        self._arrived.wait(LOGIN_TIMEOUT)
        if self._cancelled:
            raise AuthenticationError("The Qobuz login was cancelled.")
        if self._params is None:
            raise AuthenticationError("Nothing came back from Qobuz within 5 minutes.")
        job.text = "Finishing the Qobuz login"
        runner.login_qobuz(*token(qobuz_oauth_code(self._params)))
        return "Logged in to Qobuz."

    def cancel(self):
        self._cancelled = True
        self._arrived.set()


# Per service that logs in by a script: its login page, its script, and what
# the script hands back.
SCRIPT_LOGINS = frozendict(
    {
        "qobuz": (
            "https://play.qobuz.com/login",
            qobuz_navigate_snippet,
            ("user_id", "token"),
        ),
        "deezer": (DEEZER_LOGIN_URL, deezer_navigate_snippet, ("arl",)),
    }
)


class ScriptLogin:
    """A login by a short script, run on the service's site once you are logged
    in there: it reads the login the site keeps (Qobuz's token, Deezer's arl)
    and hands it back. Only one with this login's challenge is taken, once."""

    def __init__(self, service: str, callback_url: str):
        self.service, self.label = service, service.title()
        self.url, snippet, self.fields = SCRIPT_LOGINS[service]
        self.challenge = secrets.token_urlsafe(16)
        self.script = snippet(callback_url, self.challenge)
        self._arrived = threading.Event()
        self._credentials: tuple[str, ...] | None = None
        self._cancelled = False

    def receive(self, challenge: str, given: dict) -> bool:
        """The script's hand-over. True when it is this login's, and the first."""
        credentials = tuple(str(given.get(name) or "") for name in self.fields)
        if not all(credentials) or self._credentials is not None:
            return False
        if not secrets.compare_digest(challenge.encode(), self.challenge.encode()):
            return False
        self._credentials = credentials
        self._arrived.set()
        return True

    def run(self, job: Job, finish) -> str:
        """Wait for the script's hand-over, then log in with it (`finish`)."""
        job.enter("login", f"Log in to {self.label} in three steps:")
        job.offer(self.url, script=self.script)
        self._arrived.wait(LOGIN_TIMEOUT)
        job.script = ""
        if self._cancelled:
            raise AuthenticationError(f"The {self.label} login was cancelled.")
        if self._credentials is None:
            raise AuthenticationError(
                f"Nothing came back from {self.label} within 5 minutes."
            )
        job.text = f"Checking the {self.label} login"
        finish(*self._credentials)
        return f"Logged in to {self.label}."

    def cancel(self):
        self._cancelled = True
        self._arrived.set()
