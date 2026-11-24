"""Logins for the playlist tools: OAuth 2.0 with PKCE, kept in config.toml.

Spotify's login is the one streamrip's downloads use: the same app (its client
id and redirect URI in [spotify]), the same tokens, asked for the scopes both
need. Tidal's official API, which reads and changes playlists and favourites,
has a login of its own: a developer app ([tidal] api_client_id and
api_client_secret) and the api_* tokens next to the download login's.

The browser comes back to a port on this computer, caught as the Spotify
download login catches it (rip/spotify_login.py).
"""

import base64
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

from .. import spotify_auth
from .errors import LoginError, NetworkError, NotLoggedInError
from .http import error_message, json_body
lazy from ..config import Config

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Token:
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0  # unix time
    scope: str = ""

    def expired(self, margin: float = 60) -> bool:
        """True when the token has expired, or will within `margin` seconds."""
        return time.time() >= self.expires_at - margin


# Where each service's playlist login is kept: the config section, and the
# prefix of its access_token, refresh_token and token_expiry there.
TOKEN_FIELDS = frozendict({"spotify": ("spotify", ""), "tidal": ("tidal", "api_")})


class ConfigTokenStore:
    """One token per service, in the [spotify] and [tidal] sections of the
    config. A change is written to the file at once: a refresh token that
    Spotify rotates is spent once the new one arrives, and the web interface
    runs for days."""

    def __init__(self, config: Config):
        self.config = config
        # Held while a token is refreshed, so two jobs in this process cannot
        # both spend a one-time refresh token.
        self.lock = threading.Lock()

    @staticmethod
    def _fields(service: str) -> tuple[str, str]:
        try:
            return TOKEN_FIELDS[service]
        except KeyError:
            raise ValueError(f"unknown service {service!r}") from None

    def reload(self, service: str):
        """See what another program saved of this login (see Config.reload_logins)."""
        self.config.reload_logins(self._fields(service)[0])

    def load(self, service: str) -> Token | None:
        section, prefix = self._fields(service)
        c = getattr(self.config.session, section)
        access = getattr(c, f"{prefix}access_token")
        refresh = getattr(c, f"{prefix}refresh_token")
        if not access and not refresh:
            return None
        try:
            expires = float(getattr(c, f"{prefix}token_expiry") or 0)
        except ValueError:
            expires = 0.0
        return Token(access, refresh or None, expires, getattr(c, f"{prefix}scope", ""))

    def _set(self, service: str, values: dict[str, str]):
        section, prefix = self._fields(service)
        c = getattr(self.config.session, section)
        self.config.update_logins(
            section,
            {
                f"{prefix}{name}": value
                for name, value in values.items()
                if hasattr(c, f"{prefix}{name}")
            },
        )

    def save(self, service: str, token: Token, method: str = ""):
        values = {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token or "",
            "token_expiry": str(int(token.expires_at)),
            "scope": token.scope,
        }
        if method:
            # A Spotify login says how it was made: "device" needs no app of
            # the user's own, "app" uses one of the user's own. A service
            # without the field simply drops it.
            values["auth_method"] = method
        self._set(service, values)


def pkce_pair() -> tuple[str, str]:
    """A PKCE (code verifier, code challenge) pair (RFC 7636, method S256)."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OAuthClient:
    """Authorization code flow with PKCE for one service. Subclasses fill in
    the endpoints and scopes."""

    name = ""
    label = ""
    authorize_endpoint = ""
    token_endpoint = ""
    scopes: tuple[str, ...] = ()

    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        client_secret: str = "",
        session: requests.Session | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.session = session or requests.Session()

    def authorize_url(self, state: str, challenge: str) -> str:
        """Where to send the user's browser to log in."""
        return f"{self.authorize_endpoint}?" + urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.scopes),
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )

    def _token_request(
        self, data: dict[str, str], auth: tuple[str, str] | None = None
    ) -> Token:
        try:
            resp = self.session.post(
                self.token_endpoint, data=data, auth=auth, timeout=20
            )
        except requests.RequestException as exc:
            raise NetworkError(
                f"could not reach the {self.label} login server: {exc}"
            ) from exc
        if resp.status_code >= 500:  # their trouble, not a refusal
            raise NetworkError(
                f"the {self.label} login server is having trouble "
                f"(HTTP {resp.status_code})"
            )
        if not resp.ok:
            raise LoginError(
                f"{self.label} token request failed (HTTP {resp.status_code}): "
                f"{error_message(resp)}"
            )
        payload = json_body(resp)
        try:
            return Token(
                access_token=payload["access_token"],
                refresh_token=payload.get("refresh_token"),
                expires_at=time.time() + float(payload.get("expires_in") or 3600),
                scope=payload.get("scope") or "",
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A page instead of JSON, or JSON without a token.
            raise LoginError(
                f"the {self.label} login server sent an answer without a usable token"
            ) from exc

    def _with_secret(self, data: dict[str, str]) -> dict[str, str]:
        return (
            {**data, "client_secret": self.client_secret}
            if self.client_secret
            else data
        )

    def exchange(self, code: str, verifier: str) -> Token:
        """Trade the authorization code from the redirect for tokens."""
        return self._token_request(
            self._with_secret(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "client_id": self.client_id,
                    "code_verifier": verifier,
                }
            )
        )

    def refresh(self, token: Token) -> Token:
        if not token.refresh_token:
            raise LoginError(f"the {self.label} login has no refresh token")
        fresh = self._token_request(
            self._with_secret(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                    "client_id": self.client_id,
                }
            )
        )
        # A service only sends a new refresh token when it rotates them, and
        # a scope when it changed; otherwise keep the old ones.
        fresh.refresh_token = fresh.refresh_token or token.refresh_token
        fresh.scope = fresh.scope or token.scope
        return fresh

    def refresh_stored(self, store: ConfigTokenStore, current: Token) -> Token:
        """A refresh of `current`, kept in the store at once. A login shared
        outside this process (Spotify's, with the downloads) refreshes through
        its own token service instead, so the rotated refresh token is spent
        once."""
        token = self.refresh(current)
        store.save(self.name, token)
        return token

    def client_credentials(self) -> Token:
        """An app-level token (no user), for looking things up in a catalogue."""
        if not self.client_secret:
            raise LoginError(f"{self.label}: app-level access needs a client secret")
        return self._token_request(
            {"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )


def login_command(service: str) -> str:
    return f"streamrip login {'tidal-library' if service == 'tidal' else service}"


class StoredToken:
    """The logged-in user's token. Kept in memory, refreshed when it expired."""

    def __init__(self, oauth: OAuthClient, store: ConfigTokenStore):
        self.oauth = oauth
        self.store = store
        self._token: Token | None = None

    def __call__(self, force_refresh: bool = False) -> str:
        token = self._token or self.store.load(self.oauth.name)
        if token is None:
            raise NotLoggedInError(
                f"Not logged in to {self.oauth.label}. "
                f"Run: {login_command(self.oauth.name)}"
            )
        if force_refresh or token.expired():
            token = self._refresh(token)
        self._token = token
        return token.access_token

    def _refresh(self, stale: Token) -> Token:
        name = self.oauth.name
        expired = NotLoggedInError(
            f"Your {self.oauth.label} login has expired. Run: {login_command(name)}"
        )
        with self.store.lock:
            # Another job, or another program, may have refreshed this very
            # login while we waited.
            self.store.reload(name)
            current = self.store.load(name)
            if current is None:
                raise expired
            if current.access_token != stale.access_token and not current.expired():
                return current
            try:
                return self.oauth.refresh_stored(self.store, current)
            except spotify_auth.AuthServerDownError as exc:
                raise NetworkError(str(exc)) from exc
            except (LoginError, spotify_auth.SpotifyAuthError) as exc:
                raise expired from exc


class ClientCredentials:
    """An app-level token (no user), fetched on first use and kept in memory."""

    def __init__(self, oauth: OAuthClient):
        self.oauth = oauth
        self._token: Token | None = None

    def __call__(self, force_refresh: bool = False) -> str:
        if force_refresh or self._token is None or self._token.expired():
            self._token = self.oauth.client_credentials()
        return self._token.access_token
