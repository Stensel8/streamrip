"""The logins of streamrip's services, in one place: which there are, which are
in use, and logging out.

`streamrip accounts|login|logout` and `streamrip web` both ask here. The logins
themselves are kept in state.toml (see state.py).
"""

from dataclasses import dataclass

lazy from .config import Config


@dataclass(frozen=True, slots=True)
class Login:
    id: str
    label: str
    uses: str
    section: str  # the config section its fields are in
    # What a logout clears. The app (client id, secret) and what streamrip found
    # out by itself stay, so logging in again is one step.
    fields: tuple[str, ...]
    # Logged in when, of each group, at least one field is set.
    needs: tuple[tuple[str, ...], ...]
    # The developer app the user makes themselves, if the login needs one.
    app: tuple[str, ...] = ()
    # Works without it: not being logged in makes a guest, not a problem.
    guest: bool = False


LOGINS = (
    Login(
        "qobuz",
        "Qobuz",
        "downloads",
        "qobuz",
        ("user_id", "auth_token"),
        (("user_id",), ("auth_token",)),
    ),
    Login(
        "tidal",
        "Tidal",
        "downloads",
        "tidal",
        (
            "user_id",
            "country_code",
            "access_token",
            "refresh_token",
            "token_expiry",
            "token_client_id",
            "hires_access_token",
            "hires_refresh_token",
            "hires_token_expiry",
            "hires_token_client_id",
        ),
        (("access_token",),),
    ),
    Login(
        "tidal-library",
        "Tidal library",
        "playlists (needs an app of your own)",
        "tidal",
        ("api_access_token", "api_refresh_token", "api_token_expiry"),
        (("api_client_id",), ("api_access_token", "api_refresh_token")),
        app=("api_client_id", "api_client_secret"),
    ),
    Login(
        "deezer",
        "Deezer",
        "downloads",
        "deezer",
        ("arl",),
        (("arl",),),
    ),
    Login(
        "soundcloud",
        "SoundCloud",
        "downloads; a login gives better quality and higher limits",
        "soundcloud",
        ("oauth_token",),
        (("oauth_token",),),
        guest=True,
    ),
    Login(
        "spotify",
        "Spotify",
        "downloads and playlists (needs an app of your own)",
        "spotify",
        ("access_token", "refresh_token", "token_expiry", "scope"),
        (("client_id",), ("access_token", "refresh_token")),
        app=("client_id",),
    ),
)
LOGIN_IDS = tuple(login.id for login in LOGINS)
# The playlist services, by the login that lets streamrip read and change them.
LIBRARY_LOGINS = {"spotify": "spotify", "tidal": "tidal-library"}
BY_ID = {login.id: login for login in LOGINS}


@dataclass(frozen=True, slots=True)
class Status:
    login: Login
    logged_in: bool
    app_missing: bool  # it needs an app of the user's own, which is not set up


class Accounts:
    """What is logged in, from the session of a config."""

    def __init__(self, config: Config):
        self.config = config

    def _held(self, login: Login, names: tuple[str, ...]) -> bool:
        section = getattr(self.config.session, login.section)
        return any(getattr(section, name) for name in names)

    def status(self, login_id: str) -> Status:
        login = BY_ID[login_id]
        return Status(
            login,
            all(self._held(login, group) for group in login.needs),
            bool(login.app) and not all(self._held(login, (n,)) for n in login.app),
        )

    def all(self) -> list[Status]:
        return [self.status(login_id) for login_id in LOGIN_IDS]

    def logged_in(self, login_id: str) -> bool:
        return self.status(login_id).logged_in

    def logout(self, login_id: str) -> dict:
        """Forget the login. Returns what it held, for `restore`; empty if
        there was nothing to forget."""
        login = BY_ID[login_id]
        section = getattr(self.config.session, login.section)
        held = {name: getattr(section, name) for name in login.fields}
        if not any(held.values()):
            return {}
        self.config.update_logins(login.section, dict.fromkeys(login.fields, ""))
        return held

    def restore(self, login_id: str, held: dict):
        """Put back what `logout` returned (a login that did not work out)."""
        if held:
            self.config.update_logins(BY_ID[login_id].section, held)
