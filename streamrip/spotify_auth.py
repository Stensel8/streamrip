"""Spotify's login: one token, kept in the [spotify] section of the config,
used by the downloads, the playlist tools and `streamrip web` alike.

Two ways to get it:

- "device" (the default): a pair link, approved on any device. No app of
  one's own to make, no browser to send back: the only human step is
  approving one link, and it works over SSH and in a container.
- "app": your own app from the developer dashboard, OAuth with PKCE, the
  login a browser makes (`streamrip login spotify --configure`, or the
  Accounts page of streamrip web).

Spotify rotates the refresh token at every refresh, and the old one is dead
the moment the new one is back. A refresh saves its tokens at once, then, and
one lock keeps two logins in this process from spending the same one.
"""

import threading
import time
from dataclasses import dataclass

import requests

lazy from .config import Config

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
DEVICE_AUTHORIZE_URL = "https://accounts.spotify.com/oauth2/device/authorize"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# The client id the default login uses, so it needs no app of the user's own.
DEFAULT_CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"
# What an app of your own asks for: reading the user's own and shared
# playlists, for downloads (tracks, albums, artists and search need no
# scope); and reading and changing the liked songs and playlists, for the
# playlist tools. One login does both.
SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-library-modify",
    "playlist-modify-private",
    "playlist-modify-public",
)

# Seconds a token is refreshed before it actually expires.
TOKEN_MARGIN = 60
# A pair link stays open this long when Spotify does not say.
PAIR_LIFETIME = 3599
# Seconds a login server gets to answer.
TIMEOUT = 20
# The command that logs in the manual way, when the automated login fails.
CONFIGURE = "streamrip login spotify --configure"

# One login, one refresh at a time: a refresh token Spotify rotates is spent
# once the new one arrives, and two logins must not both spend the same one.
_LOCK = threading.Lock()


class SpotifyAuthError(Exception):
    """Spotify's login could not be used or made."""


class NotLoggedInError(SpotifyAuthError):
    """No Spotify login is stored yet."""


class LoginRefusedError(SpotifyAuthError):
    """Spotify refused a login, or a refresh of one."""


class DeviceLoginRefusedError(SpotifyAuthError):
    """The device login did not give a token: cancelled, expired or refused."""


class AuthServerDownError(SpotifyAuthError):
    """The login server could not be reached or is in trouble: the login
    itself is not in question, so "log in again" would be the wrong advice."""


@dataclass(frozen=True, slots=True)
class Token:
    access_token: str
    refresh_token: str
    expires_at: float  # unix time
    scope: str = ""

    def valid(self, margin: float = TOKEN_MARGIN) -> bool:
        """True while the token lasts, with `margin` seconds of leeway."""
        return bool(self.access_token) and time.time() < self.expires_at - margin


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """The pair link: open `verification_uri_complete` and approve the
    `user_code`, and the token is issued."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


# --- what the config says ------------------------------------------------------


def auth_method(config: Config) -> str:
    """How the stored login was made: "device" or "app".

    A login made before the method was recorded says it by its client id:
    one of the user's own is an app, anything else is the pair link.
    """
    c = config.session.spotify
    if c.auth_method in {"device", "app"}:
        return c.auth_method
    if c.client_id and c.client_id != DEFAULT_CLIENT_ID:
        return "app"
    return "device"


def client_id(config: Config) -> str:
    """The client id the stored login belongs to: the user's app for an app
    login, the default where there is none. A refresh must use it, or
    Spotify refuses the token it gave to another client."""
    c = config.session.spotify
    if auth_method(config) == "app" and c.client_id:
        return c.client_id
    return DEFAULT_CLIENT_ID


def load(config: Config) -> Token | None:
    """The login stored in the [spotify] section, or None for no login."""
    c = config.session.spotify
    if not c.access_token and not c.refresh_token:
        return None
    try:
        expires = float(c.token_expiry or 0)
    except ValueError:
        expires = 0.0
    return Token(c.access_token, c.refresh_token, expires, c.scope)


def save(config: Config, token: Token, method: str) -> None:
    """Keep the token in the config, at once: a rotated refresh token is dead
    once the new one is stored, and the other logins read the same file.

    `method` is how this login was made, and is kept with it. The app a login
    belongs to is not written here: where there is no app of the user's own
    the default is used, and an app login's client id is the user's, set
    before the login.
    """
    config.update_logins(
        "spotify",
        {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "token_expiry": str(int(token.expires_at)),
            "scope": token.scope,
            "auth_method": method,
        },
    )


# --- the token, refreshed when it is due ---------------------------------------


def token(config: Config, session: requests.Session | None = None) -> Token:
    """A token that works: the stored one, or a refreshed one kept in place."""
    config.reload_logins("spotify")
    current = load(config)
    if current is not None and current.valid():
        return current
    return refresh(config, session)


def refresh(
    config: Config,
    session: requests.Session | None = None,
    stale: str | None = None,
) -> Token:
    """Spend the refresh token and keep the new pair, here and in the file.

    `stale` is a token Spotify just refused: one that looks fresh on the
    clock but is not. When another login has already replaced it, its new
    token is taken instead, and the refresh token is spent once.
    """
    with _LOCK:
        # Another program (the web server, a command) may have refreshed, and
        # so rotated, this very login while we were not looking.
        config.reload_logins("spotify")
        current = load(config)
        if current is None:
            raise NotLoggedInError("Not logged in to Spotify.")
        if (stale is None and current.valid()) or (
            stale is not None and current.access_token != stale
        ):
            return current
        if not current.refresh_token:
            raise LoginRefusedError("the login has no refresh token")
        spent = current.refresh_token
        answer = _post(
            session,
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": spent,
                "client_id": client_id(config),
            },
            _verify(config),
        )
        if answer.status != 200 or "access_token" not in answer.payload:
            # Another program may have rotated the login while this refresh was
            # in flight: its pair is the one to use, and the one spent is the
            # old one.
            config.reload_logins("spotify")
            latest = load(config)
            if (
                latest is not None
                and latest.refresh_token not in {spent, ""}
                and latest.valid()
            ):
                return latest
            raise LoginRefusedError(_detail(answer))
        fresh = _token_from(answer.payload)
        # Spotify only sends a new refresh token when it rotates them, and a
        # scope when it changed; otherwise keep the old ones.
        fresh = Token(
            fresh.access_token,
            fresh.refresh_token or current.refresh_token,
            fresh.expires_at,
            fresh.scope or current.scope,
        )
        save(config, fresh, auth_method(config))
        return fresh


def _verify(config: Config | None) -> bool | None:
    """Whether the login server's certificate is checked, as the config says."""
    return config.session.downloads.verify_ssl if config is not None else None


# --- the two ways to log in -----------------------------------------------------


def request_device_code(
    config: Config | None = None, session: requests.Session | None = None
) -> DeviceCode:
    """Ask Spotify for a pair link."""
    answer = _post(
        session, DEVICE_AUTHORIZE_URL, {"client_id": DEFAULT_CLIENT_ID}, _verify(config)
    )
    if answer.status != 200 or "device_code" not in answer.payload:
        raise DeviceLoginRefusedError(_detail(answer))
    p = answer.payload
    try:
        return DeviceCode(
            device_code=str(p["device_code"]),
            user_code=str(p.get("user_code") or ""),
            verification_uri=str(
                p.get("verification_uri") or "https://spotify.com/pair"
            ),
            verification_uri_complete=str(
                p.get("verification_uri_complete") or p.get("verification_uri") or ""
            ),
            expires_in=int(p.get("expires_in") or PAIR_LIFETIME),
            interval=max(1, int(p.get("interval") or 5)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise DeviceLoginRefusedError("Spotify sent no usable device code") from e


def poll_for_token(
    device_code: str,
    config: Config | None = None,
    session: requests.Session | None = None,
    *,
    expires_in: int = PAIR_LIFETIME,
    interval: int = 5,
    stop=lambda: False,
    sleep=time.sleep,
) -> Token:
    """Wait for the user to approve the pair link, and take the token.

    `stop` ends the wait when it is set (a login given up on), and `sleep`
    is what one interval takes, in the tests a moment.
    """
    deadline = time.monotonic() + expires_in
    while True:
        if stop():
            raise DeviceLoginRefusedError("the login was cancelled")
        answer = _post(
            session,
            TOKEN_URL,
            {
                "grant_type": DEVICE_GRANT,
                "device_code": device_code,
                "client_id": DEFAULT_CLIENT_ID,
            },
            _verify(config),
        )
        if answer.status == 200 and "access_token" in answer.payload:
            return _token_from(answer.payload)
        error = answer.payload.get("error", "")
        if error not in {"authorization_pending", "slow_down"}:
            # expired_token, access_denied, and anything Spotify ends with:
            # polling on would only keep it.
            raise DeviceLoginRefusedError(_detail(answer))
        if error == "slow_down":
            interval += 2
        if time.monotonic() >= deadline:
            raise DeviceLoginRefusedError(
                "the pair link expired before it was approved; start the login again"
            )
        sleep(interval)


def exchange(
    code: str,
    verifier: str,
    redirect_uri: str,
    client_id: str,
    config: Config | None = None,
    session: requests.Session | None = None,
) -> Token:
    """Trade the code from an app login's redirect for tokens."""
    answer = _post(
        session,
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        _verify(config),
    )
    if answer.status != 200 or "access_token" not in answer.payload:
        raise LoginRefusedError(_detail(answer))
    return _token_from(answer.payload)


# --- the HTTP of it -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Answer:
    status: int
    payload: dict


def _post(
    session: requests.Session | None,
    url: str,
    data: dict[str, str],
    verify: bool | None = None,
) -> _Answer:
    """POST a form to the login server. Its trouble is an AuthServerDownError, not
    a refusal of the login."""
    session = session or requests.Session()
    if verify is not None:
        session.verify = verify
    try:
        resp = session.post(url, data=data, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise AuthServerDownError(
            f"could not reach the Spotify login server: {e}"
        ) from e
    if resp.status_code >= 500:  # their trouble, not a refusal
        raise AuthServerDownError(
            f"the Spotify login server is having trouble (HTTP {resp.status_code})"
        )
    try:
        payload = resp.json()
    except ValueError:  # a page instead of JSON
        payload = {}
    return _Answer(resp.status_code, payload if isinstance(payload, dict) else {})


def _detail(answer: _Answer) -> str:
    """Spotify's own explanation, or the status where it gives none."""
    return str(
        answer.payload.get("error_description")
        or answer.payload.get("error")
        or f"HTTP {answer.status}"
    )


def _token_from(payload: dict) -> Token:
    try:
        expires = time.time() + float(payload.get("expires_in") or 3600)
    except TypeError, ValueError:
        expires = time.time() + 3600
    return Token(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_at=expires,
        scope=str(payload.get("scope") or ""),
    )
