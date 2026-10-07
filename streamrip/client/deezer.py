import asyncio
import logging

import deezer
import requests
from deezer.errors import GWAPIError

from ..config import Config
from ..exceptions import (
    AuthenticationError,
    MissingCredentialsError,
    NonStreamableError,
)
from .client import DEFAULT_USER_AGENT, Client, new_session
from .downloadable import DeezerDownloadable

logger = logging.getLogger("streamrip")
logging.captureWarnings(True)


class DeezerClient(Client):
    """Client to handle deezer API. Does not do rate limiting.

    Attributes:
        global_config: Entire config object
        client: client from deezer py used for API requests
        logged_in: True if logged in
        config: deezer local config
        session: aiohttp.ClientSession, used only for track downloads not API requests
        max_favorites: upper bound for favorites pagination

    """

    source = "deezer"
    max_quality = 2
    max_favorites = 10_000

    def __init__(self, config: Config):
        self.global_config = config
        self.client = deezer.Deezer()
        # deezer-py's API and GW calls share this dict; its own is Chrome 79.
        self.client.http_headers["User-Agent"] = DEFAULT_USER_AGENT
        self.logged_in = False
        self.config = config.session.deezer
        self._album_cache = {}
        self._quality_warned = False

        # Increase the deezer-py requests session pool well above max_connections.
        # Each concurrent download spawns several API calls (metadata, track token,
        # GW info…) via asyncio.to_thread(), so the actual number of simultaneous
        # requests easily exceeds max_connections. pool_maxsize is just a ceiling —
        # no memory is pre-allocated — so a generous value avoids the urllib3
        # "Connection pool is full" warning without any real cost.
        # max_connections is -1 for "no limit", so never go below the default.
        max_conn = max(config.session.downloads.max_connections, 10)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max_conn,
            pool_maxsize=max(max_conn * 4, 32),
            max_retries=0,
        )
        self.client.session.mount("https://", adapter)
        self.client.session.mount("http://", adapter)

    async def login(self):
        # Used for track downloads
        self.session = new_session(
            verify_ssl=self.global_config.session.downloads.verify_ssl
        )
        arl = self.config.arl
        if not arl:
            raise MissingCredentialsError
        success = await asyncio.to_thread(self.client.login_via_arl, arl)
        if not success:
            raise AuthenticationError("Invalid or expired Deezer ARL.")
        self.logged_in = True

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        # TODO: open asyncio PR to deezer py and integrate
        if media_type == "track":
            return await self.get_track(item_id)
        elif media_type == "album":
            return await self.get_album(item_id)
        elif media_type == "playlist":
            return await self.get_playlist(item_id)
        elif media_type == "artist":
            return await self.get_artist(item_id)
        else:
            raise Exception(f"Media type {media_type} not available on deezer")

    async def get_track(self, item_id: str) -> dict:
        """A track's metadata, with its album and, when wanted, its lyrics."""
        try:
            item = await asyncio.to_thread(self.client.api.get_track, item_id)
        except Exception as e:
            raise NonStreamableError(e)

        album_id = str(item["album"]["id"])
        try:
            item["album"] = await self.get_album(album_id)
        except Exception as e:
            # Geo-restricted or removed albums: tag from the track metadata.
            logger.debug(f"Album {album_id} unavailable for track {item_id}: {e}")
            item["album"]["stream_quality"] = self._target_quality()

        if self.global_config.session.downloads.lyrics:
            try:
                lyrics_resp = await asyncio.to_thread(
                    self.client.gw.get_track_lyrics, item_id
                )
                # Use unsynced lyrics for MP3, synced for others (FLAC, OPUS, etc)
                conversion = self.global_config.session.conversion
                if conversion.enabled and conversion.codec.upper() == "MP3":
                    item["lyrics"] = lyrics_resp.get("LYRICS_TEXT") or ""
                else:
                    item["lyrics"] = self._format_synced_lyrics(
                        lyrics_resp.get("LYRICS_SYNC_JSON")
                    ) or (lyrics_resp.get("LYRICS_TEXT") or "")
            except GWAPIError as e:
                # Deezer answers with an error where it has no lyrics. That is the
                # usual case (see the notice in rip/notices.py), not a failure.
                logger.debug("Deezer has no lyrics for track %s: %s", item_id, e)
            except Exception as e:
                logger.warning(f"Failed to get lyrics for {item_id}: {e}")

        return item

    @staticmethod
    def _format_synced_lyrics(sync_json: list[dict] | None) -> str:
        """Convert Deezer's LYRICS_SYNC_JSON into LRC-formatted text."""
        if not sync_json:
            return ""
        lines = []
        for entry in sync_json:
            timestamp = entry.get("lrc_timestamp")
            if timestamp:
                lines.append(f"{timestamp}{entry.get('line', '')}")
            else:
                lines.append("")
        return "\n".join(lines)

    async def get_album(self, item_id: str) -> dict:
        """An album's metadata, tracks and quality, cached per run."""
        item_id = str(item_id)
        if item_id in self._album_cache:
            logger.debug("Deezer album cache hit for album ID %s", item_id)
            return self._album_cache[item_id]
        album_metadata, album_tracks, stream_quality = await asyncio.gather(
            asyncio.to_thread(self.client.api.get_album, item_id),
            asyncio.to_thread(self.client.api.get_album_tracks, item_id),
            self._album_quality(item_id),
            return_exceptions=True,
        )
        if isinstance(album_metadata, BaseException):
            raise NonStreamableError(album_metadata)
        if isinstance(stream_quality, BaseException):
            stream_quality = self._target_quality()
        if isinstance(album_tracks, BaseException):
            # Old album ids redirect to a re-release on the website, and the
            # API answers /album/<old id> with the new album but has no
            # /album/<old id>/tracks (upstream #893). Follow the new id.
            new_id = str(album_metadata.get("id", item_id))
            if new_id == item_id:
                raise NonStreamableError(album_tracks)
            logger.debug("Deezer album %s now lives at %s", item_id, new_id)
            album_tracks = await asyncio.to_thread(
                self.client.api.get_album_tracks, new_id
            )
        album_metadata["tracks"] = album_tracks["data"]
        album_metadata["track_total"] = len(album_tracks["data"])
        album_metadata["stream_quality"] = stream_quality
        self._album_cache[item_id] = album_metadata
        return album_metadata

    async def get_playlist(self, item_id: str) -> dict:
        if item_id.startswith("favorites:"):
            user_id = item_id.removeprefix("favorites:")
            return await self.get_user_favorites(user_id)
        try:
            pl_metadata, pl_tracks = await asyncio.gather(
                asyncio.to_thread(self.client.api.get_playlist, item_id),
                asyncio.to_thread(self.client.api.get_playlist_tracks, item_id),
            )
            pl_metadata["tracks"] = pl_tracks["data"]
            pl_metadata["track_total"] = len(pl_tracks["data"])
            return pl_metadata
        except Exception as e:
            # The public API refuses private playlists, and lately many public
            # ones too, with a PermissionException (upstream #973, #945). The
            # internal gw API accepts the ARL session and returns every track.
            logger.debug(
                "Public API refused playlist %s (%s), using gw API", item_id, e
            )
            return await self._get_playlist_gw(item_id)

    async def _get_playlist_gw(self, item_id: str) -> dict:
        try:
            page, songs = await asyncio.gather(
                asyncio.to_thread(self.client.gw.get_playlist_page, item_id),
                asyncio.to_thread(self.client.gw.get_playlist_tracks, item_id),
            )
        except Exception as e:
            raise NonStreamableError(
                f"Cannot access Deezer playlist {item_id}: {e}. Check that your "
                "ARL is valid and that the playlist still exists."
            )
        data = page.get("DATA", {}) if isinstance(page, dict) else {}
        tracks = [{"id": str(t["SNG_ID"])} for t in songs if t.get("SNG_ID")]
        return {
            "id": item_id,
            "title": data.get("TITLE") or f"Deezer playlist {item_id}",
            "tracks": tracks,
            "track_total": len(tracks),
        }

    async def get_user_favorites(self, user_id: str) -> dict:
        """Fetch the loved tracks for the authenticated Deezer account.

        ``song.getFavoriteIds`` silently caps responses at roughly 25 entries per
        call regardless of the ``nb`` parameter; the ``start`` parameter must be
        advanced by the actual count returned to paginate through all favorites.

        ``song.getFavoriteIds`` carries no ``user_id`` parameter — it always
        returns the authenticated user's favorites. Comparing ``user_id`` against
        the logged-in account to detect "other user" is unreliable for family
        accounts: ``change_account()`` shifts ``current_user`` to a child profile
        whose id differs from the main account's USER_ID that authenticated the
        ARL. The ``user_id`` argument is accepted for URL-routing compatibility
        but is not forwarded to the GW call.

        Args:
            user_id: The Deezer user ID from the profile URL. Accepted for
                routing compatibility; the GW call always uses the authenticated
                account.

        Returns:
            Playlist-shaped dict with "title", "tracks", and "track_total".
        """
        page_size = 100
        all_entries: list[dict] = []
        start = 0
        while len(all_entries) < self.max_favorites:
            response = await asyncio.to_thread(
                self.client.gw.get_user_favorite_ids, limit=page_size, start=start
            )
            entries: list[dict] = response.get("data", [])
            if not entries:
                break
            all_entries.extend(entries)
            start += len(entries)

        return {
            "title": "Loved Tracks",
            "tracks": [{"id": str(entry["SNG_ID"])} for entry in all_entries],
            "track_total": len(all_entries),
        }

    async def get_artist(self, item_id: str) -> dict:
        artist, albums = await asyncio.gather(
            asyncio.to_thread(self.client.api.get_artist, item_id),
            asyncio.to_thread(self.client.api.get_artist_albums, item_id),
        )
        artist["albums"] = albums["data"]
        return artist

    async def search(self, media_type: str, query: str, limit: int = 200) -> list[dict]:
        """Search Deezer, or fetch an editorial selection for "featured"."""
        if media_type == "featured":
            try:
                if query:
                    search_function = getattr(self.client.api, f"get_editorial_{query}")
                else:
                    search_function = self.client.api.get_editorial_releases
            except AttributeError:
                raise Exception(f'Invalid editorial selection "{query}"')
        else:
            try:
                search_function = getattr(self.client.api, f"search_{media_type}")
            except AttributeError:
                raise Exception(f"Invalid media type {media_type}")

        response = await asyncio.to_thread(search_function, query, limit=limit)  # type: ignore
        if response.get("total", 0) > 0:
            return [response]
        return []

    async def get_downloadable(
        self,
        item_id: str,
        quality: int = 2,
        is_retry: bool = False,
    ) -> DeezerDownloadable:
        if item_id is None:
            raise NonStreamableError(
                "No item id provided. This can happen when searching for fallback songs.",
            )
        # TODO: optimize such that all of the ids are requested at once
        # Deezer only has qualities 0-2; `streamrip --quality 3/4` used to IndexError.
        quality = max(0, min(quality, self.max_quality))
        dl_info: dict = {"quality": quality, "id": item_id}

        track_info = await asyncio.to_thread(self.client.gw.get_track, item_id)

        fallback_id = track_info.get("FALLBACK", {}).get("SNG_ID")

        # Preserved across the downgrade below. A fallback track is a different
        # release with its own metadata, so it should be asked for at the
        # quality the caller wanted -- not at one the replaced track fell to.
        requested_quality = quality

        quality_map = [
            (9, "MP3_128"),  # quality 0
            (3, "MP3_320"),  # quality 1
            (1, "FLAC"),  # quality 2
        ]
        # FILESIZE_* only sizes the progress bar. It says nothing reliable about
        # what Deezer will serve: it is often 0 for a format that is available,
        # so it must not pick the quality -- only get_track_url can.
        size_map = [
            int(track_info.get(f"FILESIZE_{format}", 0)) for _, format in quality_map
        ]
        dl_info["quality_to_size"] = size_map

        # Never ask for more than the subscription allows. Deezer answers that
        # with WrongLicense, which used to surface as "HiFi is required for
        # quality 2" even when quality 1 was requested on a free account
        # (upstream #1015).
        account_max = self._account_max_quality()
        if quality > account_max:
            if not self.config.lower_quality_if_not_available:
                raise NonStreamableError(
                    f"Your Deezer subscription does not allow quality {quality} "
                    f"({quality_map[quality][1]}); the maximum is {account_max}."
                )
            if not self._quality_warned:
                logger.warning(
                    "Your Deezer subscription allows at most quality %d (%s); "
                    "downloading at that quality instead of %d.",
                    account_max,
                    quality_map[account_max][1],
                    quality,
                )
                self._quality_warned = True
            quality = account_max

        # Ask Deezer for each tier from the wanted one down: a None URL means
        # this track is not served in that format.
        token = track_info["TRACK_TOKEN"]
        url = None
        for tier in range(quality, -1, -1):
            _, format_str = quality_map[tier]
            try:
                logger.debug(
                    "Fetching deezer url (%s) with token %s", format_str, token
                )
                url = await asyncio.to_thread(
                    self.client.get_track_url, token, format_str
                )
            except deezer.WrongLicense:
                if not self.config.lower_quality_if_not_available:
                    raise NonStreamableError(
                        f"Your Deezer subscription does not allow {format_str} "
                        "downloads. FLAC (quality 2) needs Deezer HiFi/Premium, "
                        "MP3 320 (quality 1) needs a paid plan.",
                    )
                continue
            except deezer.WrongGeolocation:
                if not is_retry and fallback_id:
                    return await self.get_downloadable(
                        fallback_id, quality, is_retry=True
                    )
                raise NonStreamableError(
                    "The requested track is not available. This may be due to your country/location.",
                )
            if url:
                if tier != quality:
                    logger.info(
                        "Quality %s is not available for track %s, using %s",
                        quality,
                        item_id,
                        tier,
                    )
                # The quality actually served, which fixes the file extension.
                dl_info["quality"] = tier
                break
            # A size listed but no URL is a failed request (deezer-py maps a 429
            # to None), not a format Deezer doesn't serve: stepping down would
            # silently swap in a lower one.
            if size_map[tier] > 0 or not self.config.lower_quality_if_not_available:
                break

        if not url:
            # No URL at any quality is the signature of a delisted old-catalog
            # track: it has been superseded by another release, and Deezer
            # names that release in FALLBACK.SNG_ID. Follow it, exactly as the
            # geoblock branch above does -- there the API says "not here", here
            # it says nothing at all. Recursing passes the fallback id as
            # item_id, so dl_info["id"] follows the track actually served.
            if not is_retry and fallback_id:
                logger.debug(
                    "No download URL for track %s; retrying with fallback ID %s",
                    item_id,
                    fallback_id,
                )
                return await self.get_downloadable(
                    fallback_id, requested_quality, is_retry=True
                )

            if not self.config.lower_quality_if_not_available:
                raise NonStreamableError(
                    f"The requested quality {quality} is not available and fallback is disabled."
                )

            # This used to fall back to the legacy AES-ECB CDN at
            # e-cdns-proxy-<c>.dzcdn.net. Deezer has retired those hosts and
            # none of the sixteen resolve any more, so the generated URL could
            # only ever fail at download time with a DNS error pointing at the
            # wrong culprit. Fail here instead, while we can still say why.
            raise NonStreamableError(
                "Deezer returned no download URL for this track at any quality, "
                "and it has no fallback track (delisted?). The legacy CDN that "
                "used to serve as a fallback has been retired.",
            )

        dl_info["url"] = url
        logger.debug("dz track info: %s", track_info)
        return DeezerDownloadable(self.session, dl_info)

    def _account_max_quality(self) -> int:
        """The best quality the logged-in Deezer account may stream."""
        user = getattr(self.client, "current_user", None) or {}
        if not user.get("license_token"):
            # Unknown (e.g. not logged in yet): let Deezer decide.
            return self.max_quality
        if user.get("can_stream_lossless"):
            return 2
        if user.get("can_stream_hq"):
            return 1
        return 0

    def _target_quality(self) -> int:
        """The tier tracks are asked for: the configured one, within the account."""
        wanted = max(0, min(self.config.quality, self.max_quality))
        return min(wanted, self._account_max_quality())

    async def _album_quality(self, album_id: str) -> int:
        """The tier an album comes in, for its folder name and labels.

        Tracks are asked for the target tier and fall back one by one, so the
        stable answer is the best tier every track has: one track without FLAC
        makes it an MP3 album, and the name no longer depends on which tracks
        a re-run still has to fetch. A FILESIZE of 0 can be wrong (Deezer
        reports it for formats it does serve), but this only names a folder.
        """
        target = self._target_quality()
        if target == 0 or not self.config.lower_quality_if_not_available:
            return target
        try:
            rows = await asyncio.to_thread(self.client.gw.get_album_tracks, album_id)
        except Exception as e:
            logger.debug("No file sizes for Deezer album %s: %s", album_id, e)
            return target
        # One size per tier: FILESIZE_MP3_128, FILESIZE_MP3_320, FILESIZE_FLAC.
        sizes = [
            [
                int(row.get(f"FILESIZE_{fmt}", 0))
                for fmt in ("MP3_128", "MP3_320", "FLAC")
            ]
            for row in rows
        ]
        # A track with no size at all is delisted: its fallback track decides.
        sizes = [s for s in sizes if any(s)]
        for tier in range(target, 0, -1):
            if all(s[tier] for s in sizes):
                return tier
        return 0
