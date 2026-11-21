"""Builds the logins and services of the playlist tools from streamrip's config.

The command line and the web interface both take one of these, which is also
what makes them easy to test with fakes.
"""

import json
import os
import re
import tomllib
from pathlib import Path

import requests

from .auth import ClientCredentials, ConfigTokenStore, OAuthClient, StoredToken, Token
from .errors import ConfigError, NotLoggedInError
from .http import ApiClient
from .providers.spotify import API as SPOTIFY_API
from .providers.spotify import SpotifyOAuth, SpotifyProvider, missing_scopes
from .providers.tidal import API as TIDAL_API
from .providers.tidal import JSONAPI, TidalOAuth, TidalProvider
lazy from ..config import Config
lazy from .providers.base import Provider

SERVICES = ("spotify", "tidal")
LABELS = frozendict({"spotify": "Spotify", "tidal": "Tidal"})
# Where each service's developer apps are made.
DEVELOPER_DASHBOARDS = frozendict(
    {
        "spotify": "https://developer.spotify.com/dashboard",
        "tidal": "https://developer.tidal.com/dashboard",
    }
)


def _locale_country() -> str:
    """The country of the system locale ("nl_NL.UTF-8" gives "NL"), else "US"."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if match := re.match(r"[a-z]{2,3}_([A-Z]{2})", os.environ.get(var, "")):
            return match.group(1)
    return "US"


class Services:
    """The OAuth client and the provider of each service, as the config says."""

    def __init__(self, config: Config, store: ConfigTokenStore | None = None):
        self.config = config
        self.store = store or ConfigTokenStore(config)

    # --- what the config says ----------------------------------------------

    def client_id(self, service: str) -> str:
        c = self.config.session
        return c.spotify.client_id if service == "spotify" else c.tidal.api_client_id

    def client_secret(self, service: str) -> str:
        return self.config.session.tidal.api_client_secret if service == "tidal" else ""

    def redirect_uri(self, service: str) -> str:
        c = self.config.session
        return (
            c.spotify.redirect_uri if service == "spotify" else c.tidal.api_redirect_uri
        )

    def is_configured(self, service: str) -> bool:
        return bool(self.client_id(service))

    def is_logged_in(self, service: str) -> bool:
        return self.is_configured(service) and self.store.load(service) is not None

    @property
    def country(self) -> str:
        """Which Tidal catalogue is searched: that of the account streamrip
        downloads from, else the system's country."""
        return (self.config.session.tidal.country_code or _locale_country()).upper()

    def require(self, service: str):
        """A ConfigError that says how to set the app up, unless it is."""
        if self.is_configured(service):
            return
        where = (
            "[spotify] client_id"
            if service == "spotify"
            else "[tidal] api_client_id and api_client_secret"
        )
        raise ConfigError(
            f"No {LABELS[service]} app yet. Create one at "
            f"{DEVELOPER_DASHBOARDS[service]} with the redirect URI "
            f"{self.redirect_uri(service)}, and put it in {where} of "
            f"{self.config.path} (streamrip config open), or set it up on the "
            "Accounts page of streamrip web. docs/playlists.md has the steps."
        )

    # --- building ----------------------------------------------------------

    def http(self) -> requests.Session:
        """A requests session that checks certificates as the config says."""
        session = requests.Session()
        session.verify = self.config.session.downloads.verify_ssl
        return session

    def oauth(self, service: str) -> OAuthClient:
        self.require(service)
        match service:
            case "spotify":
                return SpotifyOAuth(
                    self.client_id(service),
                    self.redirect_uri(service),
                    session=self.http(),
                )
            case "tidal":
                return TidalOAuth(
                    self.client_id(service),
                    self.redirect_uri(service),
                    self.client_secret(service),
                    session=self.http(),
                )
            case _:
                raise ValueError(f"unknown service {service!r}")

    def provider(self, service: str) -> Provider:
        oauth = self.oauth(service)
        user_token = StoredToken(oauth, self.store)
        if service == "spotify":
            token = self.store.load("spotify")
            if token and (missing := missing_scopes(token.scope)):
                raise NotLoggedInError(
                    "Your Spotify login was made for downloading only; the playlist "
                    f"tools also need {', '.join(missing)}. Log in again: "
                    "streamrip playlist login spotify"
                )
            return SpotifyProvider(
                ApiClient(SPOTIFY_API, user_token, session=self.http())
            )
        # Tidal: catalogue lookups need an app-level token, which needs the secret.
        catalog = (
            ApiClient(
                TIDAL_API,
                ClientCredentials(oauth),
                headers=JSONAPI,
                session=self.http(),
            )
            if oauth.client_secret
            else None
        )
        user = ApiClient(TIDAL_API, user_token, headers=JSONAPI, session=self.http())
        return TidalProvider(user, catalog, self.country)


def music_sync_dir() -> Path:
    """Where Music-Sync kept its settings and logins."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "music-sync"


def migrate_music_sync(services: Services, folder: Path | None = None) -> list[str]:
    """Take the apps and logins of Music-Sync's config.toml and tokens.json
    into streamrip's config, where streamrip has none of its own. Returns what
    was taken, for people.

    A Spotify login only comes along with its app: a token is the app's own.
    The redirect URIs do not come along (Music-Sync's were on port 8888, the
    web interface's): the apps need streamrip's added, which `status` and the
    Accounts page show.
    """
    folder = folder or music_sync_dir()
    try:
        settings = tomllib.loads((folder / "config.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"No Music-Sync settings in {folder}.") from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Could not read Music-Sync's settings: {e}") from e
    try:
        tokens = json.loads((folder / "tokens.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        tokens = {}
    if not isinstance(tokens, dict):
        tokens = {}

    def text(section: str, key: str) -> str:
        value = (settings.get(section) or {}).get(key, "")
        return value.strip() if isinstance(value, str) else ""

    config, taken = services.config, []
    spotify_id, tidal_id = text("spotify", "client_id"), text("tidal", "client_id")
    ours = config.session
    same_spotify_app = spotify_id and ours.spotify.client_id in {"", spotify_id}
    if spotify_id and not ours.spotify.client_id:
        for data in (config.session, config.file):
            data.spotify.client_id = spotify_id
        taken.append("the Spotify app")
    if tidal_id and not ours.tidal.api_client_id:
        for data in (config.session, config.file):
            data.tidal.api_client_id = tidal_id
            data.tidal.api_client_secret = text("tidal", "client_secret")
        taken.append("the Tidal app")
    config.file.set_modified()
    config.save_file()

    for service, allowed in (("spotify", same_spotify_app), ("tidal", bool(tidal_id))):
        saved = tokens.get(service)
        if not allowed or not isinstance(saved, dict) or services.store.load(service):
            continue
        try:
            token = Token(**saved)
        except TypeError:
            continue
        services.store.save(service, token)
        taken.append(f"the {LABELS[service]} login")
    return taken
