"""Spotify: the track lists, tags and covers, and the audio.

The track lists, tags and covers come from Spotify's Web API. The audio comes
from one of two backends (the config's `backend`): "spotify" plays the track
with Spotify's own headless client (Soloist, see `soloist`), at the account's
quality, and streamrip records the local audio as lossless FLAC; "youtube"
finds the same recording on YouTube Music (see `ytmusic`) for yt-dlp to
download, the way spotDL does. A track the Spotify backend cannot serve is
routed to YouTube Music either way.

The login is the user's own app from the developer dashboard, whose owner
must have Premium (a rule since February 2026), logged in with OAuth and PKCE
(see the login section below). The API keeps the development-mode rules: at
most 10 search results per request; playlist tracks under "item"; and only
playlists the user owns or collaborates on can be read.
"""

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

from ..console import plural
from ..exceptions import (
    APIError,
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from .audio_match import MatchTrack
from .client import Client
from .downloadable import SoloistDownloadable, YtDlpDownloadable
from .ytmusic import YouTubeMusicMatcher
lazy from ..config import Config
lazy from . import soloist

logger = logging.getLogger("streamrip")

API_URL = "https://api.spotify.com/v1"

PAGE_SIZE = 50
# Apps in development mode get at most 10 results per search request, and the
# albums of an artist answer "Invalid limit" to more than that as well.
SEARCH_PAGE_SIZE = 10
# A menu of more results than this is five requests, and no one reads them all.
SEARCH_MAX_RESULTS = 50

DEVELOPER_DASHBOARD = "https://developer.spotify.com/dashboard"
# Since February 2026 Spotify refuses apps whose owner has no Premium, with a 403.
PREMIUM_REQUIRED = (
    "Spotify only lets apps whose owner has a Premium subscription use its API "
    "(a rule since February 2026). Someone with Premium can make the app and add "
    "your account under Settings > User Management in the developer dashboard."
)
PLAYLIST_NOT_READABLE = (
    "Spotify only lets an app read the tracks of playlists you own or collaborate "
    "on, not the ones you merely follow. Copy the playlist to one of your own, "
    "and download that."
)


class SpotifyAPIError(APIError):
    """Spotify answered a request with an error status."""

    def __init__(self, status: int, message: str):
        """Keep the HTTP status next to Spotify's own explanation."""
        super().__init__(message)
        self.status = status


def pkce_pair() -> tuple[str, str]:
    """A PKCE (code verifier, code challenge) pair (RFC 7636, method S256)."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def plain_id(item: str) -> str:
    """The id in a Spotify id, URI ("spotify:track:...") or open.spotify.com URL."""
    return re.split(r"[:/]", item.split("?", 1)[0].rstrip("/"))[-1]


def _images(images: list[dict] | None) -> dict[str, str]:
    """Cover urls by the size names the search results use, largest first."""
    urls = [
        i["url"]
        for i in sorted(images or [], key=lambda i: i.get("width") or 0, reverse=True)
        if i.get("url")
    ]
    if not urls:
        return {}
    middle = urls[min(1, len(urls) - 1)]
    return {"large": urls[0], "medium": middle, "small": middle, "thumbnail": urls[-1]}


def _names(artists: list[dict] | None) -> list[dict]:
    return [{"name": a["name"]} for a in artists or [] if a.get("name")]


RECORD_TYPES = frozendict(
    {"album": "ALBUM", "single": "SINGLE", "compilation": "COMPILE"}
)


def summary_item(media_type: str, item: dict) -> dict:
    """A search result in the shape streamrip's search menu reads."""
    images = _images((item.get("album") or item).get("images"))
    if media_type == "track":
        album = item.get("album") or {}
        return {
            "id": item["id"],
            "title": item.get("name") or "Unknown",
            "artists": _names(item.get("artists")),
            "album": {"title": album.get("name"), "image": images},
            "release_date": album.get("release_date"),
            "explicit": bool(item.get("explicit")),
            "duration": (item.get("duration_ms") or 0) // 1000,
            "track_number": item.get("track_number"),
        }
    if media_type == "album":
        artists = _names(item.get("artists"))
        return {
            "id": item["id"],
            "title": item.get("name") or "Unknown",
            "artists": artists,
            "artist": {"name": ", ".join(a["name"] for a in artists)},
            "release_date": item.get("release_date"),
            "tracks_count": item.get("total_tracks"),
            "record_type": RECORD_TYPES.get(item.get("album_type") or ""),
            "image": images,
        }
    if media_type == "artist":
        return {
            "id": item["id"],
            "name": item.get("name") or "Unknown",
            "followers_count": (item.get("followers") or {}).get("total"),
            "image": images,
        }
    owner = item.get("owner") or {}
    # February 2026 renamed a playlist's "tracks" summary to "items".
    summary = item.get("items") if isinstance(item.get("items"), dict) else None
    summary = summary or item.get("tracks") or {}
    return {
        "id": item["id"],
        "name": item.get("name") or "Unknown",
        "owner": {"name": owner.get("display_name") or owner.get("id")},
        "tracks_count": summary.get("total"),
        "description": item.get("description"),
        "image": images,
    }


# --- the login ------------------------------------------------------------------
#
# One token, kept in the [spotify] section of the config, used by the downloads,
# the playlist tools and `streamrip web` alike. It comes from the user's own app
# from the developer dashboard, with OAuth and PKCE: the login a browser makes
# (`streamrip login spotify`, or the Accounts page of streamrip web).
#
# Spotify rotates the refresh token at every refresh, and the old one is dead
# the moment the new one is back. A refresh saves its tokens at once, then, and
# one lock keeps two logins in this process from spending the same one.


AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
# Off, but could be handy: the client id the pair link login used, so it needed
# no app of the user's own. Everyone who used it shared one rate limit, which
# was mostly used up (HTTP 429). `git log -S DEFAULT_CLIENT_ID` finds the login.
# DEFAULT_CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"  # noqa: ERA001
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
# Seconds a login server gets to answer.
AUTH_TIMEOUT = 20

# One login, one refresh at a time: a refresh token Spotify rotates is spent
# once the new one arrives, and two logins must not both spend the same one.
_REFRESH_LOCK = threading.Lock()


class SpotifyAuthError(Exception):
    """Spotify's login could not be used or made."""


class NotLoggedInError(SpotifyAuthError):
    """No Spotify login is stored yet."""


class LoginRefusedError(SpotifyAuthError):
    """Spotify refused a login, or a refresh of one."""


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


# --- what the config says ------------------------------------------------------


def load_login(config: Config) -> Token | None:
    """The login stored in the [spotify] section, or None for no login."""
    c = config.session.spotify
    if not c.access_token and not c.refresh_token:
        return None
    try:
        expires = float(c.token_expiry or 0)
    except ValueError:
        expires = 0.0
    return Token(c.access_token, c.refresh_token, expires, c.scope)


def save_login(config: Config, token: Token) -> None:
    """Keep the token in the config, at once: a rotated refresh token is dead
    once the new one is stored, and the other logins read the same file. The
    app it belongs to is the user's own, set before the login."""
    config.update_logins(
        "spotify",
        {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "token_expiry": str(int(token.expires_at)),
            "scope": token.scope,
        },
    )


# --- the token, refreshed when it is due ---------------------------------------


def login_token(config: Config, session: requests.Session | None = None) -> Token:
    """A token that works: the stored one, or a refreshed one kept in place."""
    config.reload_logins("spotify")
    current = load_login(config)
    if current is not None and current.valid():
        return current
    return refresh_login(config, session)


def refresh_login(
    config: Config,
    session: requests.Session | None = None,
    stale: str | None = None,
) -> Token:
    """Spend the refresh token and keep the new pair, here and in the file.

    `stale` is a token Spotify just refused: one that looks fresh on the
    clock but is not. When another login has already replaced it, its new
    token is taken instead, and the refresh token is spent once.
    """
    with _REFRESH_LOCK:
        # Another program (the web server, a command) may have refreshed, and
        # so rotated, this very login while we were not looking.
        config.reload_logins("spotify")
        current = load_login(config)
        if current is None:
            raise NotLoggedInError("Not logged in to Spotify.")
        if not (client_id := config.session.spotify.client_id):
            raise NotLoggedInError("Spotify needs an app of your own to log in with.")
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
                "client_id": client_id,
            },
            _verify(config),
        )
        if answer.status != 200 or "access_token" not in answer.payload:
            # Another program may have rotated the login while this refresh was
            # in flight: its pair is the one to use, and the one spent is the
            # old one.
            config.reload_logins("spotify")
            latest = load_login(config)
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
        save_login(config, fresh)
        return fresh


def _verify(config: Config | None) -> bool | None:
    """Whether the login server's certificate is checked, as the config says."""
    return config.session.downloads.verify_ssl if config is not None else None


# --- the login a browser makes ----------------------------------------------------


def exchange_code(
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
        resp = session.post(url, data=data, timeout=AUTH_TIMEOUT)
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


class SpotifyClient(Client):
    """Spotify: the user's own Spotify app; the audio from Spotify itself
    (the `spotify` backend, its official client, recorded locally) or a
    YouTube Music match (the `youtube` one)."""

    source = "spotify"
    # A 429 is the app's rate limit: wait as Spotify says, once, then say so.
    api_attempts = 2

    def __init__(self, config: Config):
        super().__init__(config)
        self.matcher = YouTubeMusicMatcher(match_videos=self.config.match_videos)
        # Track responses by id: a playlist already carries its tracks in full,
        # and a track is fetched once for its tags and once for its audio.
        self._tracks: dict[str, dict] = {}
        # The Soloist backend this process uses for Spotify's own audio,
        # or None: built on first use; it keeps nothing open between runs.
        self._soloist_backend = None

    @property
    def container(self) -> str:
        """The container the downloaded audio ends up in, as albums name it:
        FLAC when Spotify's own audio is on, else what the YouTube download is."""
        if self.soloist_backend is not None:
            return "FLAC"
        return "MP3" if self.config.audio_format.lower() == "mp3" else "AAC"

    def _audio(self) -> dict:
        """What an album's files are, for its folder name and labels: the
        container, and for Spotify's own audio the 24-bit/44.1 kHz it is
        recorded and saved as (the YouTube download is lossy: no bits to name)."""
        if self.soloist_backend is None:
            return {"container": self.container}
        return {"container": "FLAC", "bit_depth": 24, "sampling_rate": 44.1}

    # --- Spotify's own audio (the `spotify` backend) ---------------------------

    @property
    def soloist_backend(self):
        """This client's Soloist backend, or None: the backend off, the
        Soloist API key not in the config, or a computer that cannot record
        (a container has no sound server), and then the audio goes to
        YouTube Music instead."""
        c = self.config
        if c.backend != "spotify" or not c.soloist_api_key:
            return None
        if self._soloist_backend is None:
            backend = soloist.SoloistBackend(c)
            if not backend.supported:
                return None
            self._soloist_backend = backend
        return self._soloist_backend

    async def pair_soloist(self, api_key: str) -> str:
        """Pair the Soloist device once, and keep its API key in the config.

        The Web-API login already happened; this only adds the pairing the
        Spotify backend plays back. The prompter says the steps; the pairing
        itself blocks, so it runs in a worker thread. Returns the username.
        """
        self.config.soloist_api_key = api_key
        # A backend built without the key would keep its empty one.
        self._soloist_backend = None
        backend = self.soloist_backend
        if backend is None:
            if api_key and self.config.backend == "spotify":
                raise AuthenticationError(
                    "This computer cannot do Spotify's own audio: that needs "
                    "Linux with PipeWire or PulseAudio (a container has neither)."
                )
            raise AuthenticationError(
                "Spotify's own audio needs a Soloist API key (streamrip login spotify)."
            )
        stop = threading.Event()
        pairing = asyncio.ensure_future(asyncio.to_thread(backend.pair, stop))
        try:
            return await asyncio.shield(pairing)
        except asyncio.CancelledError:
            # Ctrl-C: the thread cannot be cancelled, so tell it to end the
            # pairing, and wait for that rather than for its 10 minutes.
            stop.set()
            while not pairing.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({pairing})
            raise

    # The pace and the hourly line are for files straight from a CDN; this
    # audio plays as long as it lasts, or comes from YouTube Music, and the
    # limit is the app's, waited out (_pause_requests). A CDN backend would
    # be paced again.
    paced = False

    async def close(self):
        """The Spotify backend keeps nothing open between runs: nothing to close."""
        await super().close()

    def _pause_requests(self, seconds: float):
        """Spotify's limit is the app's, not a sign of an account in danger:
        wait as Spotify says. The pacing and the ban guard are Tidal's."""
        owner = self._paced_owner()
        owner._retry_at = max(owner._retry_at, time.monotonic() + seconds)
        logger.warning(
            "Spotify is rate-limiting your app (HTTP 429); it asks for %.0fs.",
            seconds,
        )

    # --- login -------------------------------------------------------------

    async def login(self):
        """Use the saved login, refreshing its token if needed.

        Raises MissingCredentialsError when there is no login yet (the prompter
        takes the user through one), and AuthenticationError when Spotify no
        longer accepts the saved one.
        """
        self._ensure_session()
        if not self.config.client_id:
            # A login left over from the pair link has no app: it cannot be renewed.
            raise MissingCredentialsError(
                "Spotify needs an app of your own to log in with."
            )
        try:
            await asyncio.to_thread(login_token, self.global_config)
        except NotLoggedInError as e:
            raise MissingCredentialsError(str(e)) from e
        except AuthServerDownError as e:
            raise AuthenticationError(str(e)) from e
        except SpotifyAuthError as e:
            raise AuthenticationError(
                f"Spotify did not accept the login ({e}). Log in again to fix it."
            ) from e
        self.logged_in = True

    def authorization_url(self) -> tuple[str, str, str]:
        """Where to send the user to log in: (url, state, PKCE code verifier)."""
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(SCOPES),
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        return f"{AUTHORIZE_URL}?{query}", state, verifier

    async def finish_login(self, code: str, verifier: str):
        """Trade the code from the login redirect for tokens, and log in."""
        c = self.config
        try:
            token = await asyncio.to_thread(
                exchange_code,
                code,
                verifier,
                c.redirect_uri,
                c.client_id,
                self.global_config,
            )
        except AuthServerDownError as e:
            raise AuthenticationError(str(e)) from e
        except SpotifyAuthError as e:
            raise AuthenticationError(
                f"Spotify did not accept the login ({e}). Log in again to fix it."
            ) from e
        save_login(self.global_config, token)
        self.logged_in = True

    async def _fresh_token(self, stale: str | None = None) -> str:
        """An access token that works. `stale` is one that was just rejected.

        The token service keeps the rotated pair at once, and takes the pair
        another login in this process already made, so a refresh token is
        spent once and the newest pair is the one that is kept.
        """
        token = await asyncio.to_thread(refresh_login, self.global_config, stale=stale)
        return token.access_token

    # --- API ---------------------------------------------------------------

    async def _api(self, path: str, params: dict | None = None) -> dict:
        """GET from Spotify's API. `path` may be a "next page" url as it came,
        as long as it is on the API: the access token goes along, and an
        address that only a response names is not to get it.
        """
        if not path.startswith("http"):
            url = f"{API_URL}/{path}"
        elif path.startswith(f"{API_URL}/"):
            url = path
        else:
            raise APIError("Spotify sent a next page link that is not on its API.")
        token = await self._fresh_token()
        for attempt in (1, 2):
            status, body = await self._get_json(
                url, params, {"Authorization": f"Bearer {token}"}
            )
            if status == 401 and attempt == 1:
                # The token may have just expired: one new one, then a retry.
                token = await self._fresh_token(stale=token)
                continue
            break
        self._raise_for_status(status, body)
        return body

    def _raise_for_status(self, status: int, body: dict):
        if status < 400:
            return
        error = body.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        message = message or (error if isinstance(error, str) else None) or ""
        if status == 401:
            raise AuthenticationError(
                "Spotify no longer accepts the login. Log in again to fix it."
            )
        if status == 403 and "premium" in message.lower():
            raise SpotifyAPIError(status, PREMIUM_REQUIRED)
        if status == 404:
            raise ItemNotFoundError(message or "Spotify has no such item")
        if status == 429:
            raise SpotifyAPIError(
                status,
                "Spotify keeps answering with 'too many requests'. Its limit is "
                "per app: wait a minute and try again. Lowering "
                "requests_per_minute in [spotify] helps too.",
            )
        if status == 410:
            # 410 is final, on purpose: it is not in RETRY_STATUSES, so this
            # answer costs one request. Retrying in a row only extends the
            # block, so say wait -- not "try again".
            raise SpotifyAPIError(
                status,
                "Spotify is answering with HTTP 410 (Gone): it is "
                "temporarily refusing this app and account from this "
                "address. The account is fine -- if you can still stream "
                "on open.spotify.com, this is a temporary block, not a "
                "login problem. Do not retry now; repeated attempts extend "
                "the block. Wait a few hours and try again. If it holds on, "
                "make a fresh app in the developer dashboard and log in "
                "with it.",
            )
        raise SpotifyAPIError(status, f"Spotify error (HTTP {status}): {message}")

    async def _pages(self, path: str, params: dict | None = None):
        """Every page of a list endpoint, following the "next" links."""
        page = await self._api(path, params)
        while True:
            yield page
            if not (next_url := page.get("next")):
                return
            page = await self._api(next_url)

    # --- metadata ----------------------------------------------------------

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        """The metadata of a track, album, artist or playlist, with track lists.

        An album, a playlist and an artist come back with their tracks (or, for
        an artist, albums) as a list under "tracks" ("albums").
        """
        item_id = plain_id(item_id)
        match media_type:
            case "track":
                return await self._get_track(item_id)
            case "album":
                return await self._get_album(item_id)
            case "playlist":
                return await self._get_playlist(item_id)
            case "artist":
                return await self._get_artist(item_id)
        raise ValueError(f"Spotify has no {media_type}s")

    def _remember(self, track: dict) -> dict:
        """Keep a track response for reuse, with the container of its audio."""
        track.setdefault("album", {}).update(self._audio())
        self._tracks[track["id"]] = track
        return track

    async def _get_track(self, item_id: str) -> dict:
        if (track := self._tracks.get(item_id)) is not None:
            return track
        return self._remember(await self._api(f"tracks/{item_id}"))

    async def _get_album(self, item_id: str) -> dict:
        album = await self._api(f"albums/{item_id}")
        first = album.get("tracks") or {}
        tracks = list(first.get("items") or [])
        if next_url := first.get("next"):
            async for page in self._pages(next_url):
                tracks.extend(page.get("items") or [])
        album["tracks"] = [t for t in tracks if t and t.get("id")]
        album.update(self._audio())
        return album

    async def _get_playlist(self, item_id: str) -> dict:
        skipped = 0
        try:
            playlist = await self._api(f"playlists/{item_id}")
            tracks: list[dict] = []
            async for page in self._pages(
                f"playlists/{item_id}/items", {"limit": PAGE_SIZE}
            ):
                for entry in filter(None, page.get("items") or []):
                    # The track sits under "item" since February 2026, it used
                    # to be "track". Local files and episodes cannot be matched.
                    track = entry.get("item") or entry.get("track")
                    if (
                        track
                        and track.get("type", "track") == "track"
                        and not track.get("is_local")
                        and track.get("id")
                    ):
                        tracks.append(self._remember(track))
                    else:
                        skipped += 1
        except ItemNotFoundError:
            raise NonStreamableError(PLAYLIST_NOT_READABLE) from None
        except SpotifyAPIError as e:
            # Spotify answers a playlist it will not show with a 403 (or a 404,
            # above). A 403 about Premium is another matter.
            if e.status == 403 and str(e) != PREMIUM_REQUIRED:
                raise NonStreamableError(PLAYLIST_NOT_READABLE) from None
            raise
        playlist["name"] = playlist.get("name") or f"Spotify playlist {item_id}"
        playlist["tracks"] = tracks
        if skipped:
            logger.info(
                "Skipped %s of %s: local files, podcast episodes and tracks "
                "Spotify no longer shows cannot be downloaded",
                plural(skipped, "item"),
                playlist["name"],
            )
        return playlist

    async def _get_artist(self, item_id: str) -> dict:
        artist = await self._api(f"artists/{item_id}")
        albums: dict[str, dict] = {}
        # The artist's own releases: not the ones they only appear on.
        async for page in self._pages(
            f"artists/{item_id}/albums",
            {"include_groups": "album,single", "limit": SEARCH_PAGE_SIZE},
        ):
            for album in filter(None, page.get("items") or []):
                if album.get("id"):
                    albums.setdefault(album["id"], album)
        artist["albums"] = list(albums.values())
        return artist

    async def search(
        self,
        media_type: str,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Search results as pages of {"items": [...]}, in the shape of the menu."""
        self.check_search_type(media_type)
        wanted = min(limit, SEARCH_MAX_RESULTS)
        pages = []
        while wanted > 0:
            size = min(SEARCH_PAGE_SIZE, wanted)
            data = await self._api(
                "search",
                {"q": query, "type": media_type, "limit": size, "offset": offset},
            )
            block = data.get(f"{media_type}s") or {}
            items = [i for i in block.get("items") or [] if i and i.get("id")]
            pages.append({"items": [summary_item(media_type, i) for i in items]})
            if not items or not block.get("next"):
                break
            offset += size
            wanted -= size
        return pages

    # --- audio -------------------------------------------------------------

    async def get_downloadable(self, item_id: str, quality: int):
        """The audio of a track: from Spotify itself where the backend is
        `spotify` (its official client plays the track, streamrip records
        it), else its best match on YouTube Music, for yt-dlp."""
        track = await self._get_track(plain_id(item_id))
        # The backend remembers the tracks Spotify refused to play; those go
        # straight to YouTube Music, even mid-run.
        if (backend := self.soloist_backend) is not None and plain_id(
            item_id
        ) not in backend.refused:
            # The audio is what Spotify plays for the account, whatever
            # audio_format is; it ends up FLAC: stamp the album's quality
            # bracket so the folder name says so.
            track.setdefault("album", {})["container"] = "FLAC"
            return SoloistDownloadable(
                backend, plain_id(item_id), track.get("duration_ms") or 0, self.source
            )
        return await self._youtube_downloadable(track)

    async def youtube_downloadable(self, item_id: str):
        """The track's best match on YouTube Music, whatever the backend: the
        fallback when Spotify's own audio failed for any reason."""
        return await self._youtube_downloadable(
            await self._get_track(plain_id(item_id))
        )

    async def _youtube_downloadable(self, track: dict):
        album = track.get("album") or {}
        wanted = MatchTrack(
            title=track.get("name") or "",
            artists=[a["name"] for a in track.get("artists") or [] if a.get("name")],
            album=album.get("name") or "",
            duration_ms=track.get("duration_ms"),
            explicit=bool(track.get("explicit")),
        )
        isrc = (track.get("external_ids") or {}).get("isrc")
        match = await self.matcher.find(wanted, isrc.upper() if isrc else None)
        if match is None:
            raise NonStreamableError(f"No match on YouTube Music for {wanted}")
        c = self.config
        return YtDlpDownloadable(
            self.session,
            match.url,
            c.audio_format.lower().lstrip("."),
            c.audio_bitrate,
            self.source,
            self.global_config.session.downloads.verify_ssl,
        )
