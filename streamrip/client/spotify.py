"""Spotify: the track lists, tags and covers. The audio comes from YouTube Music.

Spotify encrypts its own audio, so this client only reads Spotify's Web API for
what a track *is*, then finds the same recording on YouTube Music
(see `ytmusic`) for yt-dlp to download, the way spotDL does.

How the login and the API are used follows the rules for apps in development
mode that Spotify has had since February 2026, as Music-Sync
(https://github.com/Stensel8/Music-Sync) handles them: an app of the user's own,
whose owner has Premium; OAuth with PKCE, so a client id is all it needs; at
most 10 search results per request; playlist tracks under "item"; and only
playlists the user owns or collaborates on can be read.
"""

import asyncio
import base64
import hashlib
import logging
import os
import re
import secrets
import time
from urllib.parse import urlencode

import aiohttp

from ..config import Config
from ..exceptions import (
    APIError,
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from .audio_match import MatchTrack
from .client import Client, new_session
from .downloadable import YtDlpDownloadable
from .ytmusic import YouTubeMusicMatcher

logger = logging.getLogger("streamrip")

API_URL = "https://api.spotify.com/v1"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
# Only what reading the user's own and shared playlists takes. Tracks, albums,
# artists and search need no scope.
SCOPES = ("playlist-read-private", "playlist-read-collaborative")

PAGE_SIZE = 50
# Apps in development mode get at most 10 results per search request, and the
# albums of an artist answer "Invalid limit" to more than that as well.
SEARCH_PAGE_SIZE = 10
# A menu of more results than this is five requests, and no one reads them all.
SEARCH_MAX_RESULTS = 50
# A token is refreshed this long before it expires.
TOKEN_MARGIN = 60

MEDIA_TYPES = ("track", "album", "artist", "playlist")

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


RECORD_TYPES = {"album": "ALBUM", "single": "SINGLE", "compilation": "COMPILE"}


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


class SpotifyClient(Client):
    source = "spotify"
    logged_in = False

    def __init__(self, config: Config):
        """A client for the user's Spotify app, finding audio on YouTube Music."""
        self.global_config = config
        self.config = config.session.spotify
        self.rate_limiter = self.get_rate_limiter(
            config.session.downloads.requests_per_minute,
        )
        self.matcher = YouTubeMusicMatcher(match_videos=self.config.match_videos)
        # Track responses by id: a playlist already carries its tracks in full,
        # and a track is fetched once for its tags and once for its audio.
        self._tracks: dict[str, dict] = {}
        self._token_lock = asyncio.Lock()

    @property
    def container(self) -> str:
        """The container the downloaded audio ends up in, as albums name it."""
        return "MP3" if self.config.audio_format.lower() == "mp3" else "AAC"

    # --- login -------------------------------------------------------------

    def ensure_session(self):
        """Open the HTTP session, if this client has none yet."""
        if getattr(self, "session", None) is None:
            self.session = new_session(
                verify_ssl=self.global_config.session.downloads.verify_ssl
            )

    async def login(self):
        """Use the saved login, refreshing its token if needed.

        Raises MissingCredentialsError when there is no login yet (the prompter
        takes the user through one), and AuthenticationError when Spotify no
        longer accepts the saved one.
        """
        c = self.config
        if not c.client_id:
            raise MissingCredentialsError(
                "No Spotify client id: see [spotify] in the config."
            )
        if not c.refresh_token:
            raise MissingCredentialsError("Not logged in to Spotify.")
        self.ensure_session()
        if not self._token_valid():
            await self._refresh()
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
        self.ensure_session()
        await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "client_id": self.config.client_id,
                "code_verifier": verifier,
            }
        )
        self.logged_in = True

    def _token_valid(self) -> bool:
        c = self.config
        try:
            expiry = float(c.token_expiry)
        except ValueError:
            return False
        return bool(c.access_token) and time.time() < expiry - TOKEN_MARGIN

    async def _refresh(self):
        await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.config.refresh_token,
                "client_id": self.config.client_id,
            }
        )

    async def _token_request(self, data: dict):
        """Ask the token endpoint for tokens and keep them, here and in the file."""
        async with self.session.post(TOKEN_URL, data=data) as resp:
            try:
                payload = await resp.json(content_type=None)
            except ValueError, aiohttp.ClientError:
                payload = {}
            if resp.status != 200 or "access_token" not in payload:
                detail = (
                    payload.get("error_description")
                    or payload.get("error")
                    or f"HTTP {resp.status}"
                )
                raise AuthenticationError(
                    f"Spotify did not accept the login ({detail}). "
                    "Log in again to fix it."
                )
        expiry = str(int(time.time() + float(payload.get("expires_in", 3600))))
        # Spotify only sends a new refresh token when it rotates them.
        refresh = payload.get("refresh_token") or self.config.refresh_token
        for c in (self.config, self.global_config.file.spotify):
            c.access_token = payload["access_token"]
            c.refresh_token = refresh
            c.token_expiry = expiry
        self.global_config.file.set_modified()

    async def _fresh_token(self, stale: str | None = None) -> str:
        """An access token that works. `stale` is one that was just rejected."""
        async with self._token_lock:
            token = self.config.access_token
            # Another request may have refreshed it while this one waited.
            if token and token != stale and self._token_valid():
                return token
            await self._refresh()
            return self.config.access_token

    # --- API ---------------------------------------------------------------

    @staticmethod
    async def _read(resp: aiohttp.ClientResponse) -> tuple[int, dict]:
        """(status, JSON) of a response; the JSON is {} for a body with none."""
        try:
            body = await resp.json(content_type=None)
        except ValueError, aiohttp.ClientError:
            body = {}
        return resp.status, body if isinstance(body, dict) else {}

    async def _api(self, path: str, params: dict | None = None) -> dict:
        """GET from Spotify's API. `path` may be a "next page" url as it came."""
        url = path if path.startswith("http") else f"{API_URL}/{path}"
        token = await self._fresh_token()
        for attempt in (1, 2):
            status, body = await self._get_with_retries(
                url, self._read, params, {"Authorization": f"Bearer {token}"}
            )
            if status == 401 and attempt == 1:
                # The token may have just expired: one new one, then a retry.
                token = await self._fresh_token(stale=token)
                continue
            break
        self._raise_for_status(status, body)
        return body

    @staticmethod
    def _raise_for_status(status: int, body: dict):
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
                "Spotify keeps answering with 'too many requests'. If this is "
                "its quota, wait a while. Lowering requests_per_minute in "
                "[downloads] helps too.",
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
        if media_type == "track":
            return await self._get_track(item_id)
        if media_type == "album":
            return await self._get_album(item_id)
        if media_type == "playlist":
            return await self._get_playlist(item_id)
        if media_type == "artist":
            return await self._get_artist(item_id)
        raise NotImplementedError(f"Spotify has no {media_type}s to download")

    def _remember(self, track: dict) -> dict:
        """Keep a track response for reuse, with the container of its audio."""
        track.setdefault("album", {})["container"] = self.container
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
        album["container"] = self.container
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
                f"Skipped {skipped} item(s) of {playlist['name']}: local files, "
                "podcast episodes and tracks Spotify no longer shows cannot be downloaded"
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
        if media_type not in MEDIA_TYPES:
            raise APIError(f"Cannot search Spotify for {media_type}s")
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

    async def get_downloadable(self, item_id: str, quality: int = 0):
        """The audio of a track: its best match on YouTube Music, for yt-dlp."""
        track = await self._get_track(plain_id(item_id))
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
