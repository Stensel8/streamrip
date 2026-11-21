"""Builds the logins and services of the playlist tools from streamrip's config.

The command line and the web interface both take one of these, which is also
what makes them easy to test with fakes.
"""

import os
import re

import requests

from .auth import ClientCredentials, ConfigTokenStore, OAuthClient, StoredToken
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
