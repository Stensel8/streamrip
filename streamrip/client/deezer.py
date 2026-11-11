import asyncio
import json
import logging
import re

lazy import aiohttp
lazy from yarl import URL

from ..exceptions import (
    AuthenticationError,
    MissingCredentialsError,
    NonStreamableError,
)
from .client import Client, register_header_dict
from .downloadable import DeezerDownloadable
lazy from ..config import Config

logger = logging.getLogger("streamrip")

# Deezer's formats by quality tier.
FORMATS = ("MP3_128", "MP3_320", "FLAC")


class DeezerError(Exception):
    """An error Deezer answered a request with."""


class WrongLicenseError(DeezerError):
    """The account's subscription doesn't allow the format."""


class WrongGeolocationError(DeezerError):
    """The track isn't available in the account's country."""


class _Deezer:
    """What streamrip uses of Deezer, over the client's aiohttp session: the
    public API (`api`), the website's own API (`gw`), which takes the ARL login,
    and the media API that gives a track's download URL.
    """

    def __init__(self, owner: DeezerClient):
        self._owner = owner
        # Follows the rotating browser identity (register_header_dict).
        self.http_headers: dict[str, str] = {}
        self.current_user: dict = {}
        self.api = _PublicAPI(self)
        self.gw = _GatewayAPI(self)

    async def request(self, method: str, url: str, **kwargs) -> dict:
        """JSON from Deezer, trying twice more after a lost connection."""
        for attempt in range(3):
            try:
                async with self._owner.session.request(
                    method, url, headers=self.http_headers, **kwargs
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            except aiohttp.ClientConnectionError, TimeoutError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2)
        raise AssertionError("unreachable")

    async def login_via_arl(self, arl: str) -> bool:
        """Log in with the ARL cookie; False if Deezer doesn't take it.

        The licence of a family account's main user is the one every profile
        streams with, so its own profiles need no switching here.
        """
        self._owner.session.cookie_jar.update_cookies(
            {"arl": arl.strip()}, URL("https://www.deezer.com")
        )
        user = (await self.gw.call("deezer.getUserData")).get("USER") or {}
        if not user.get("USER_ID"):
            return False
        options = user["OPTIONS"]
        self.current_user = {
            "id": user["USER_ID"],
            "license_token": options["license_token"],
            "can_stream_hq": options["web_hq"] or options["mobile_hq"],
            "can_stream_lossless": options["web_lossless"]
            or options["mobile_lossless"],
            "country": options["license_country"],
        }
        language = (user.get("SETTING") or {}).get("global", {}).get("language", "")
        if language := re.sub(r"[^0-9A-Za-z *,-.;=]", "", str(language)):
            self.http_headers["Accept-Language"] = language
        return True

    async def get_track_url(self, track_token: str, track_format: str) -> str | None:
        """A track's download URL in a format, or None if Deezer gives none."""
        user = self.current_user
        if not user.get("license_token"):
            return None
        if (track_format == "FLAC" and not user.get("can_stream_lossless")) or (
            track_format == "MP3_320" and not user.get("can_stream_hq")
        ):
            raise WrongLicenseError(track_format)
        body = {
            "license_token": user["license_token"],
            "media": [
                {
                    "type": "FULL",
                    "formats": [{"cipher": "BF_CBC_STRIPE", "format": track_format}],
                }
            ],
            "track_tokens": [track_token],
        }
        try:
            resp = await self.request(
                "POST", "https://media.deezer.com/v1/get_url", json=body
            )
        except aiohttp.ClientResponseError:
            return None  # a failed request, not an answer (see get_downloadable)
        for data in resp.get("data") or []:
            if errors := data.get("errors"):
                if errors[0].get("code") == 2002:
                    raise WrongGeolocationError(user.get("country"))
                raise DeezerError(json.dumps(errors))
            media = data.get("media") or []
            return media[0]["sources"][0]["url"] if media else None
        return None


class _PublicAPI:
    """Deezer's public API, api.deezer.com."""

    def __init__(self, dz: _Deezer):
        self._dz = dz

    async def call(self, path: str, params: dict | None = None) -> dict:
        """An answer of the API; its quota and busy errors (4, 700) are waited
        out for up to a minute, so a long queue still gets through.
        """
        for attempt in range(13):
            body = await self._dz.request(
                "GET", f"https://api.deezer.com/{path}", params=params or {}
            )
            if not (error := body.get("error")):
                return body
            if error.get("code") in {4, 700} and attempt < 12:
                await asyncio.sleep(5)
                continue
            raise DeezerError(f"{path}: {error.get('message') or error}")
        raise AssertionError("unreachable")

    async def get_track(self, track_id) -> dict:
        return await self.call(f"track/{track_id}")

    async def get_album(self, album_id) -> dict:
        return await self.call(f"album/{album_id}")

    async def get_album_tracks(self, album_id) -> dict:
        return await self.call(f"album/{album_id}/tracks", {"index": 0, "limit": -1})

    async def get_playlist(self, playlist_id) -> dict:
        return await self.call(f"playlist/{playlist_id}")

    async def get_playlist_tracks(self, playlist_id) -> dict:
        return await self.call(
            f"playlist/{playlist_id}/tracks", {"index": 0, "limit": -1}
        )

    async def get_artist(self, artist_id) -> dict:
        return await self.call(f"artist/{artist_id}")

    async def get_artist_albums(self, artist_id) -> dict:
        return await self.call(f"artist/{artist_id}/albums", {"index": 0, "limit": -1})

    async def search(self, media_type: str, query: str, limit: int) -> dict:
        return await self.call(f"search/{media_type}", {"q": query, "limit": limit})


class _GatewayAPI:
    """The website's own API (gw-light), which takes the ARL login."""

    URL = "https://www.deezer.com/ajax/gw-light.php"

    def __init__(self, dz: _Deezer):
        self._dz = dz
        self._token: str | None = None

    async def call(self, method: str, args: dict | None = None) -> dict:
        """A method's results. A stale token is renewed, and a FALLBACK the
        answer names is followed, a couple of times at most.
        """
        args = dict(args or {})
        for _ in range(3):
            if self._token is None and method != "deezer.getUserData":
                self._token = (await self.call("deezer.getUserData"))["checkForm"]
            params = {
                "api_version": "1.0",
                "input": "3",
                "method": method,
                "api_token": "null" if method == "deezer.getUserData" else self._token,
            }
            body = await self._dz.request("POST", self.URL, params=params, json=args)
            error = body.get("error")
            if not error:
                results = body["results"]
                if method == "deezer.getUserData" and self._token is None:
                    self._token = results.get("checkForm")
                return results
            if error in (
                {"GATEWAY_ERROR": "invalid api token"},
                {"VALID_TOKEN_REQUIRED": "Invalid CSRF token"},
            ):
                self._token = None
                continue
            if fallback := (body.get("payload") or {}).get("FALLBACK"):
                args.update(fallback)
                continue
            raise DeezerError(f"{method}: {json.dumps(error)}")
        raise DeezerError(f"{method}: no answer after renewing the token")

    async def get_track(self, track_id) -> dict:
        return await self.call("song.getData", {"SNG_ID": track_id})

    async def get_album_tracks(self, album_id) -> list[dict]:
        resp = await self.call("song.getListByAlbum", {"ALB_ID": album_id, "nb": -1})
        return resp["data"]

    async def get_playlist_page(self, playlist_id) -> dict:
        args = {"PLAYLIST_ID": playlist_id, "lang": "en", "header": True, "tab": 0}
        return await self.call("deezer.pagePlaylist", args)

    async def get_playlist_tracks(self, playlist_id) -> list[dict]:
        resp = await self.call(
            "playlist.getSongs", {"PLAYLIST_ID": playlist_id, "nb": -1}
        )
        return resp["data"]

    async def get_user_favorite_ids(self, limit: int, start: int) -> dict:
        args = {"nb": limit, "start": start, "checksum": None}
        return await self.call("song.getFavoriteIds", args)

    async def get_user_albums(self, user_id, limit: int) -> list[dict]:
        args = {"USER_ID": user_id, "tab": "albums", "nb": limit}
        page = await self.call("deezer.pageProfile", args)
        return [{"id": a["ALB_ID"]} for a in page["TAB"]["albums"]["data"]]


class DeezerClient(Client):
    """Deezer: an ARL login and its API (self.client), and the audio, all over
    aiohttp (self.session)."""

    source = "deezer"
    max_quality = 2
    # upper bound for favorites pagination
    max_favorites = 10_000

    def __init__(self, config: Config):
        super().__init__(config)
        self.client = _Deezer(self)
        # Registered to follow the browser identity as it rotates.
        register_header_dict(self.client.http_headers)
        self._album_cache = {}
        self._quality_warned = False

    async def login(self):
        self._ensure_session()
        arl = self.config.arl
        if not arl:
            raise MissingCredentialsError
        success = await self.client.login_via_arl(arl)
        if not success:
            raise AuthenticationError("Invalid or expired Deezer ARL.")
        self.logged_in = True

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        match media_type:
            case "track":
                return await self._get_track(item_id)
            case "album":
                return await self._get_album(item_id)
            case "playlist":
                return await self._get_playlist(item_id)
            case "artist":
                return await self._get_artist(item_id)
            case "label" if str(item_id).startswith("favorites:"):
                return await self._get_user_favorite_albums(
                    item_id.removeprefix("favorites:")
                )
        raise ValueError(f"Deezer has no {media_type}s")

    async def _get_track(self, item_id: str) -> dict:
        """A track's metadata, with its album."""
        try:
            item = await self.client.api.get_track(item_id)
        except Exception as e:
            raise NonStreamableError(e) from e

        album_id = str(item["album"]["id"])
        try:
            item["album"] = await self._get_album(album_id)
        except Exception as e:
            # Geo-restricted or removed albums: tag from the track metadata.
            logger.debug("Album %s unavailable for track %s: %s", album_id, item_id, e)
            item["album"]["stream_quality"] = self._target_quality()

        return item

    async def _get_album(self, item_id: str) -> dict:
        """An album's metadata, tracks and quality, cached per run."""
        item_id = str(item_id)
        if item_id in self._album_cache:
            logger.debug("Deezer album cache hit for album ID %s", item_id)
            return self._album_cache[item_id]
        album_metadata, album_tracks, stream_quality = await asyncio.gather(
            self.client.api.get_album(item_id),
            self.client.api.get_album_tracks(item_id),
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
            album_tracks = await self.client.api.get_album_tracks(new_id)
        album_metadata["tracks"] = album_tracks["data"]
        album_metadata["track_total"] = len(album_tracks["data"])
        album_metadata["stream_quality"] = stream_quality
        self._album_cache[item_id] = album_metadata
        return album_metadata

    async def _get_playlist(self, item_id: str) -> dict:
        item_id = str(item_id)
        if item_id.startswith("favorites:"):
            user_id = item_id.removeprefix("favorites:")
            return await self._get_user_favorites(user_id)
        try:
            pl_metadata, pl_tracks = await asyncio.gather(
                self.client.api.get_playlist(item_id),
                self.client.api.get_playlist_tracks(item_id),
            )
            tracks = pl_tracks["data"]
        except Exception as e:
            # The public API refuses private playlists, and lately many public
            # ones too, with a PermissionException (upstream #973, #945). The
            # internal gw API accepts the ARL session and returns every track.
            logger.debug(
                "Public API refused playlist %s (%s), using gw API", item_id, e
            )
            return await self._get_playlist_gw(item_id)
        pl_metadata["tracks"] = tracks
        pl_metadata["track_total"] = len(tracks)
        return pl_metadata

    async def _get_playlist_gw(self, item_id: str) -> dict:
        try:
            page, songs = await asyncio.gather(
                self.client.gw.get_playlist_page(item_id),
                self.client.gw.get_playlist_tracks(item_id),
            )
        except Exception as e:
            raise NonStreamableError(
                f"Cannot access Deezer playlist {item_id}: {e}. Check that your "
                "ARL is valid and that the playlist still exists."
            ) from e
        data = page.get("DATA", {}) if isinstance(page, dict) else {}
        tracks = [{"id": str(t["SNG_ID"])} for t in songs if t.get("SNG_ID")]
        return {
            "id": item_id,
            "title": data.get("TITLE") or f"Deezer playlist {item_id}",
            "tracks": tracks,
            "track_total": len(tracks),
        }

    async def _get_user_favorites(self, user_id: str) -> dict:
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
            response = await self.client.gw.get_user_favorite_ids(
                limit=page_size, start=start
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

    async def _get_user_favorite_albums(self, user_id: str) -> dict:
        """The albums a profile has liked, label-shaped for LabelMetadata."""
        albums = await self.client.gw.get_user_albums(user_id, limit=self.max_favorites)
        return {
            "name": "Loved Albums",
            "albums": [{"id": str(a["id"])} for a in albums],
        }

    async def _get_artist(self, item_id: str) -> dict:
        artist, albums = await asyncio.gather(
            self.client.api.get_artist(item_id),
            self.client.api.get_artist_albums(item_id),
        )
        artist["albums"] = albums["data"]
        return artist

    async def search(self, media_type: str, query: str, limit: int = 200) -> list[dict]:
        if media_type not in {"track", "album", "artist", "playlist"}:
            raise ValueError(f"Cannot search Deezer for {media_type}s")
        response = await self.client.api.search(media_type, query, limit)
        if response.get("total", 0) > 0:
            return [response]
        return []

    async def get_downloadable(
        self, item_id: str, quality: int, *, is_retry: bool = False
    ) -> DeezerDownloadable:
        if item_id is None:
            raise NonStreamableError(
                "No item id provided. This can happen when searching for fallback songs.",
            )
        # Deezer only has qualities 0-2; `streamrip --quality 3/4` used to IndexError.
        quality = max(0, min(quality, self.max_quality))

        track_info = await self.client.gw.get_track(item_id)

        fallback_id = track_info.get("FALLBACK", {}).get("SNG_ID")

        # Preserved across the downgrade below. A fallback track is a different
        # release with its own metadata, so it should be asked for at the
        # quality the caller wanted -- not at one the replaced track fell to.
        requested_quality = quality

        # FILESIZE_* only sizes the progress bar. It says nothing reliable about
        # what Deezer will serve: it is often 0 for a format that is available,
        # so it must not pick the quality -- only get_track_url can.
        size_map = [int(track_info.get(f"FILESIZE_{f}", 0)) for f in FORMATS]

        # Never ask for more than the subscription allows. Deezer answers that
        # with WrongLicenseError, which used to surface as "HiFi is required for
        # quality 2" even when quality 1 was requested on a free account
        # (upstream #1015).
        account_max = self._account_max_quality()
        if quality > account_max:
            if not self.config.lower_quality_if_not_available:
                raise NonStreamableError(
                    f"Your Deezer subscription does not allow quality {quality} "
                    f"({FORMATS[quality]}); the maximum is {account_max}."
                )
            if not self._quality_warned:
                logger.warning(
                    "Your Deezer subscription allows at most quality %d (%s); "
                    "downloading at that quality instead of %d.",
                    account_max,
                    FORMATS[account_max],
                    quality,
                )
                self._quality_warned = True
            quality = account_max

        # Ask Deezer for each tier from the wanted one down: a None URL means
        # this track is not served in that format.
        token = track_info["TRACK_TOKEN"]
        url = None
        for tier in range(quality, -1, -1):
            format_str = FORMATS[tier]
            try:
                logger.debug(
                    "Fetching deezer url (%s) with token %s", format_str, token
                )
                url = await self.client.get_track_url(token, format_str)
            except WrongLicenseError:
                if not self.config.lower_quality_if_not_available:
                    raise NonStreamableError(
                        f"Your Deezer subscription does not allow {format_str} "
                        "downloads. FLAC (quality 2) needs Deezer HiFi/Premium, "
                        "MP3 320 (quality 1) needs a paid plan.",
                    ) from None
                continue
            except WrongGeolocationError:
                if not is_retry and fallback_id:
                    return await self.get_downloadable(
                        fallback_id, quality, is_retry=True
                    )
                raise NonStreamableError(
                    "The requested track is not available. This may be due to your country/location.",
                ) from None
            if url:
                if tier != quality:
                    logger.info(
                        "Quality %s is not available for track %s, using %s",
                        quality,
                        item_id,
                        tier,
                    )
                # The quality actually served, which fixes the file extension.
                served = tier
                break
            # A size listed but no URL is a failed request (get_track_url maps a
            # 429 to None), not a format Deezer doesn't serve: stepping down
            # would silently swap in a lower one.
            if size_map[tier] > 0 or not self.config.lower_quality_if_not_available:
                break

        if not url:
            # No URL at any quality is the signature of a delisted old-catalog
            # track: it has been superseded by another release, and Deezer
            # names that release in FALLBACK.SNG_ID. Follow it, exactly as the
            # geoblock branch above does -- there the API says "not here", here
            # it says nothing at all. Recursing passes the fallback id as
            # item_id, so the downloadable's id follows the track served.
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

            # Fail here, while we can still say why, rather than guess a URL
            # that could only fail at download time.
            raise NonStreamableError(
                "Deezer returned no download URL for this track at any quality, "
                "and it has no fallback track (delisted?).",
            )

        logger.debug("dz track info: %s", track_info)
        return DeezerDownloadable(
            self.session, url=url, quality=served, track_id=item_id, sizes=size_map
        )

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
            rows = await self.client.gw.get_album_tracks(album_id)
        except Exception as e:
            logger.debug("No file sizes for Deezer album %s: %s", album_id, e)
            return target
        # One size per tier: FILESIZE_MP3_128, FILESIZE_MP3_320, FILESIZE_FLAC.
        sizes = [[int(row.get(f"FILESIZE_{f}", 0)) for f in FORMATS] for row in rows]
        # A track with no size at all is delisted: its fallback track decides.
        sizes = [s for s in sizes if any(s)]
        for tier in range(target, 0, -1):
            if all(s[tier] for s in sizes):
                return tier
        return 0
