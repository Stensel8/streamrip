"""Spotify: the track lists, tags and covers, and the audio.

The track lists, tags and covers come from Spotify's Web API. The audio comes
from one of two backends (the config's `backend`): "spotify" plays the track
with Spotify's own headless client (Soloist, see `soloist`), at the account's
quality, and streamrip records the local audio as lossless FLAC; "youtube"
finds the same recording on YouTube Music (see `ytmusic`) for yt-dlp to
download, the way spotDL does. A track the Spotify backend cannot serve is
routed to YouTube Music either way.

The login is one of two (spotify_auth): the "device" one, Spotify's own
desktop client, which needs no app of the user's own and is the default, or
the "app" one, the user's own app from the developer dashboard, whose owner
must have Premium (a rule since February 2026), logged in with OAuth and PKCE.
The API keeps the development-mode rules either way: at most 10 search results
per request; playlist tracks under "item"; and only playlists the user owns or
collaborates on can be read.
"""

import asyncio
import base64
import hashlib
import logging
import os
import re
import secrets
from urllib.parse import urlencode

from .. import spotify_auth
from ..console import plural
from ..exceptions import (
    APIError,
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from ..spotify_auth import AUTHORIZE_URL, SCOPES
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


class SpotifyClient(Client):
    """Spotify: the user's own Spotify app; the audio from Spotify itself
    (the `spotify` backend, its official client, recorded locally) or a
    YouTube Music match (the `youtube` one)."""

    source = "spotify"

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
        """The container the downloaded audio ends up in, as albums name it."""
        return "MP3" if self.config.audio_format.lower() == "mp3" else "AAC"

    # --- Spotify's own audio (the `spotify` backend) ---------------------------

    @property
    def soloist_backend(self):
        """This client's Soloist backend, or None: the backend off, or the
        Soloist API key not in the config, and then the audio goes to
        YouTube Music instead."""
        c = self.config
        if c.backend != "spotify" or not c.soloist_api_key:
            return None
        if self._soloist_backend is None:
            self._soloist_backend = soloist.SoloistBackend(c)
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
            raise AuthenticationError(
                "Spotify's own audio needs the Soloist API key in the config."
            )
        return await asyncio.to_thread(backend.pair)

    async def close(self):
        """The Spotify backend keeps nothing open between runs: nothing to close."""
        await super().close()

    # --- login -------------------------------------------------------------

    async def login(self):
        """Use the saved login, refreshing its token if needed.

        Raises MissingCredentialsError when there is no login yet (the prompter
        takes the user through one), and AuthenticationError when Spotify no
        longer accepts the saved one.
        """
        self._ensure_session()
        try:
            await asyncio.to_thread(spotify_auth.token, self.global_config)
        except spotify_auth.NotLoggedInError as e:
            raise MissingCredentialsError(str(e)) from e
        except spotify_auth.AuthServerDownError as e:
            raise AuthenticationError(str(e)) from e
        except spotify_auth.SpotifyAuthError as e:
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
                spotify_auth.exchange,
                code,
                verifier,
                c.redirect_uri,
                c.client_id,
                self.global_config,
            )
        except spotify_auth.AuthServerDownError as e:
            raise AuthenticationError(str(e)) from e
        except spotify_auth.SpotifyAuthError as e:
            raise AuthenticationError(
                f"Spotify did not accept the login ({e}). Log in again to fix it."
            ) from e
        spotify_auth.save(self.global_config, token, "app")
        self.logged_in = True

    async def _fresh_token(self, stale: str | None = None) -> str:
        """An access token that works. `stale` is one that was just rejected.

        The token service keeps the rotated pair at once, and takes the pair
        another login in this process already made, so a refresh token is
        spent once and the newest pair is the one that is kept.
        """
        token = await asyncio.to_thread(
            spotify_auth.refresh, self.global_config, stale=stale
        )
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
                "[spotify] helps too.",
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
        if media_type not in MEDIA_TYPES:
            raise ValueError(f"Cannot search Spotify for {media_type}s")
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
