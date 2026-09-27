import asyncio
import base64
import json
import logging
import re
import time
from json import JSONDecodeError

import aiohttp

from ..config import Config
from ..exceptions import (
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from .client import Client
from .downloadable import TidalDASHDownloadable, TidalDownloadable

logger = logging.getLogger("streamrip")

BASE = "https://api.tidalhifi.com/v1"
AUTH_URL = "https://auth.tidal.com/v1/oauth2"


def _b64(s: str) -> str:
    return base64.b64decode(s).decode("iso-8859-1")


# Tidal decides which formats a client may stream per OAuth client id, and no
# single id is known to be served everything (see upstream PR #1017):
#
#   default   FLAC 16/44.1 for every lossless release, never hi-res
#   hi-res    24-bit FLAC (MPEG-DASH) for hi-res releases, but AAC 320 for
#             ordinary lossless releases
#
# The default gives lossless for everything. Set [tidal] hires_client = true
# in the config to prefer 24-bit instead, or client_id/client_secret to use
# any other pair (e.g. after Tidal revokes one).
DEFAULT_CLIENT_ID = _b64("NE4zbjZRMXg5NUxMNUs3cA==")
DEFAULT_CLIENT_SECRET = _b64(
    "b0tPWGZKVzM3MWNYNnhhWjBQeWhnR05CZE5MbEJaZDRBS0tZb3VnTWppaz0="
)
HIRES_CLIENT_ID = _b64("ZlgySnhkbW50WldLMGl4VA==")
HIRES_CLIENT_SECRET = _b64(
    "MU5tNUFmREFqeHJnSkZKYktOV0xlQXlLR1ZHbUlOdVhQUExIVlhBdnhBZz0="
)
STREAM_URL_REGEX = re.compile(
    r"#EXT-X-STREAM-INF:BANDWIDTH=\d+,AVERAGE-BANDWIDTH=\d+,CODECS=\"(?!jpeg)[^\"]+\",RESOLUTION=\d+x\d+\n(.+)"
)

QUALITY_MAP = {
    0: "LOW",  # AAC
    1: "HIGH",  # AAC
    2: "LOSSLESS",  # CD Quality
    3: "HI_RES",  # Best available: 24-bit FLAC (DASH) where the client may
}


class TidalClient(Client):
    """TidalClient."""

    source = "tidal"
    max_quality = 3

    def __init__(self, config: Config):
        self.logged_in = False
        self.global_config = config
        self.config = config.session.tidal
        self.rate_limiter = self.get_rate_limiter(
            config.session.downloads.requests_per_minute,
        )
        c = self.config
        if c.client_id and c.client_secret:
            self.client_id, self.client_secret = c.client_id, c.client_secret
        elif c.hires_client:
            self.client_id, self.client_secret = HIRES_CLIENT_ID, HIRES_CLIENT_SECRET
        else:
            self.client_id = DEFAULT_CLIENT_ID
            self.client_secret = DEFAULT_CLIENT_SECRET
        # HTTP Basic auth for the token endpoint. Built by hand because
        # aiohttp.BasicAuth is deprecated as of aiohttp 3.14.
        credentials = f"{self.client_id}:{self.client_secret}".encode()
        self.auth_headers = {
            "Authorization": "Basic " + base64.b64encode(credentials).decode()
        }

    async def login(self):
        self.session = await self.get_session(
            verify_ssl=self.global_config.session.downloads.verify_ssl
        )
        c = self.config
        if not c.access_token:
            raise MissingCredentialsError(
                "No Tidal access token in config -- Tidal has not been set up yet."
            )
        if c.token_client_id != self.client_id:
            # Tokens are bound to the OAuth client that issued them. A token
            # from another client (an older streamrip, or before client_id was
            # changed in the config) keeps that client's format entitlements
            # and cannot be refreshed, so a fresh login is needed.
            raise MissingCredentialsError(
                "The saved Tidal login belongs to a different client id "
                "(streamrip now uses one that serves lossless tracks as FLAC "
                "instead of AAC, or the config changed it). Please log in again."
            )

        try:
            self.token_expiry = float(c.token_expiry)
        except TypeError, ValueError:
            self.token_expiry = 0.0
        self.refresh_token = c.refresh_token

        if self.token_expiry - time.time() < 86400:  # 1 day
            await self._refresh_access_token()
        else:
            await self._login_by_access_token(c.access_token, c.user_id)

        self.logged_in = True

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        """Send a request to the api for information.

        :param item_id:
        :type item_id: str
        :param media_type: track, album, playlist, or video.
        :type media_type: str
        :rtype: dict
        """
        assert media_type in (
            "track",
            "album",
            "playlist",
            "video",
            "artist",
        ), media_type

        url = f"{media_type}s/{item_id}"
        item = await self._api_request(url)
        if media_type in ("playlist", "album"):
            # TODO: move into new method and make concurrent
            resp = await self._api_request(f"{url}/items")
            tracks_left = item["numberOfTracks"]
            if tracks_left > 100:
                offset = 0
                while tracks_left > 0:
                    offset += 100
                    tracks_left -= 100
                    items_resp = await self._api_request(
                        f"{url}/items", {"offset": offset}
                    )
                    resp["items"].extend(items_resp["items"])

            item["tracks"] = [item["item"] for item in resp["items"]]
        elif media_type == "artist":
            logger.debug("filtering eps")
            album_resp, ep_resp = await asyncio.gather(
                self._api_request(f"{url}/albums"),
                self._api_request(f"{url}/albums", params={"filter": "EPSANDSINGLES"}),
            )

            item["albums"] = album_resp["items"]
            item["albums"].extend(ep_resp["items"])
        elif media_type == "track" and self.global_config.session.downloads.lyrics:
            try:
                resp = await self._api_request(
                    f"tracks/{item_id!s}/lyrics", base="https://tidal.com/v1"
                )

                # Use unsynced lyrics for MP3, synced for others (FLAC, OPUS, etc)
                if (
                    self.global_config.session.conversion.enabled
                    and self.global_config.session.conversion.codec.upper() == "MP3"
                ):
                    item["lyrics"] = resp.get("lyrics") or ""
                else:
                    item["lyrics"] = resp.get("subtitles") or resp.get("lyrics") or ""
            except ItemNotFoundError:
                # Most tracks simply have no lyrics. That is the expected
                # answer, not a problem worth reporting.
                logger.debug("No lyrics available for track %s", item_id)
            except (
                NonStreamableError,
                TypeError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as e:
                # Lyrics that should have been there but could not be
                # fetched -- worth knowing about, never worth the track. Any
                # request failure counts: a 401 from the lyrics endpoint used
                # to escape here and abort the whole download (#959).
                logger.warning(f"Failed to get lyrics for {item_id}: {e}")

        logger.debug(item)
        return item

    async def search(self, media_type: str, query: str, limit: int = 100) -> list[dict]:
        """Search for a query.

        :param query:
        :type query: str
        :param media_type: track, album, playlist, or video.
        :type media_type: str
        :param limit: max is 100
        :type limit: int
        :rtype: dict
        """
        params = {
            "query": query,
            "limit": limit,
        }
        assert media_type in ("album", "track", "playlist", "video", "artist")
        resp = await self._api_request(f"search/{media_type}s", params=params)
        # A single hit is still a result: last.fm playlists search with limit=1.
        if len(resp.get("items") or []) > 0:
            return [resp]
        return []

    async def get_downloadable(self, track_id: str, quality: int):
        quality = max(0, min(quality, self.max_quality))
        params = {
            "audioquality": QUALITY_MAP[quality],
            "playbackmode": "STREAM",
            "assetpresentation": "FULL",
        }
        resp = await self._api_request(
            f"tracks/{track_id}/playbackinfopostpaywall", params
        )
        logger.debug(resp)
        if "manifest" not in resp:
            raise NonStreamableError(
                resp.get("userMessage") or f"No stream available for track {track_id}"
            )

        # Hi-res (HI_RES_LOSSLESS) tracks are served as an MPEG-DASH manifest
        # rather than the usual base64 JSON; handle it instead of silently
        # falling back to a lower quality.
        if resp.get("manifestMimeType") == "application/dash+xml":
            return self._get_downloadable_from_dash(track_id, resp)

        try:
            manifest = json.loads(base64.b64decode(resp["manifest"]).decode("utf-8"))
        except JSONDecodeError, UnicodeDecodeError, ValueError:
            if quality <= 0:
                raise NonStreamableError(f"Could not read manifest for {track_id}")
            logger.warning(
                f"Failed to get manifest for {track_id}. Retrying with lower quality."
            )
            return await self.get_downloadable(track_id, quality - 1)

        logger.debug(manifest)
        enc_key = manifest.get("keyId")
        if manifest.get("encryptionType") == "NONE":
            enc_key = None
        return TidalDownloadable(
            self.session,
            url=(manifest.get("urls") or [None])[0],
            codec=manifest["codecs"],
            encryption_key=enc_key,
            restrictions=manifest.get("restrictions"),
        )

    def _get_downloadable_from_dash(self, track_id: str, resp: dict):
        """Build a downloadable from a Tidal MPEG-DASH manifest.

        From upstream PR #998. The manifest lists an init segment and a
        SegmentTimeline; every media segment URL is derived from the template.
        """
        import xml.etree.ElementTree as ET

        dash_xml = base64.b64decode(resp["manifest"]).decode("utf-8")
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
        root = ET.fromstring(dash_xml)

        representation = root.find(".//mpd:Representation", ns)
        if representation is None:
            raise NonStreamableError(f"No Representation in DASH manifest ({track_id})")
        codecs = representation.get("codecs") or "flac"

        seg_template = representation.find("mpd:SegmentTemplate", ns)
        if seg_template is None:
            seg_template = root.find(".//mpd:SegmentTemplate", ns)
        if seg_template is None:
            raise NonStreamableError(
                f"No SegmentTemplate in DASH manifest ({track_id})"
            )

        init_url = seg_template.get("initialization")
        media_template = seg_template.get("media")
        start_number = int(seg_template.get("startNumber", "1"))
        timeline = seg_template.find("mpd:SegmentTimeline", ns)
        if not init_url or not media_template or timeline is None:
            raise NonStreamableError(f"Incomplete DASH manifest ({track_id})")

        segment_count = sum(
            int(s.get("r", "0")) + 1 for s in timeline.findall("mpd:S", ns)
        )
        segment_urls = [
            media_template.replace("$Number$", str(n))
            for n in range(start_number, start_number + segment_count)
        ]
        logger.debug(
            "DASH manifest for %s: codecs=%s, %d segments",
            track_id,
            codecs,
            len(segment_urls),
        )
        return TidalDASHDownloadable(
            self.session,
            init_url=init_url,
            segment_urls=segment_urls,
            codec=codecs,
        )

    async def get_video_file_url(self, video_id: str) -> str:
        """Get the HLS video stream url.

        The stream is downloaded using ffmpeg for now.

        :param video_id:
        :type video_id: str
        :rtype: str
        """
        params = {
            "videoquality": "HIGH",
            "playbackmode": "STREAM",
            "assetpresentation": "FULL",
        }
        resp = await self._api_request(
            f"videos/{video_id}/playbackinfopostpaywall", params=params
        )
        manifest = json.loads(base64.b64decode(resp["manifest"]).decode("utf-8"))
        async with self.session.get(manifest["urls"][0]) as resp:
            available_urls = await resp.text(encoding="utf-8")

        # Highest resolution is last
        *_, last_match = STREAM_URL_REGEX.finditer(available_urls)

        return last_match.group(1)

    # ---------- Login Utilities ---------------

    async def _login_by_access_token(self, token: str, user_id: str):
        """Login using the access token.

        Used after the initial authorization.

        :param token: access token
        :param user_id: To verify that the user is correct
        """
        headers = {"authorization": f"Bearer {token}"}  # temporary
        async with self.session.get(
            "https://api.tidal.com/v1/sessions",
            headers=headers,
        ) as _resp:
            resp = await _resp.json()

        if resp.get("status", 200) != 200:
            raise Exception(f"Login failed {resp}")

        if str(resp.get("userId")) != str(user_id):
            raise Exception(f"User id mismatch {resp['userId']} v {user_id}")

        c = self.config
        c.user_id = resp["userId"]
        c.country_code = resp["countryCode"]
        c.access_token = token
        self._update_authorization_from_config()

    async def _get_login_link(self) -> str:
        data = {
            "client_id": self.client_id,
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = await self._api_post(f"{AUTH_URL}/device_authorization", data)

        if resp.get("status", 200) != 200:
            raise Exception(f"Device authorization failed {resp}")

        device_code = resp["deviceCode"]
        return f"https://{device_code}"

    def _update_authorization_from_config(self):
        self.session.headers.update(
            {"authorization": f"Bearer {self.config.access_token}"},
        )

    async def _get_auth_status(self, device_code) -> tuple[int, dict[str, int | str]]:
        """Check if the user has logged in inside the browser.

        returns (status, authentication info)
        """
        data = {
            "client_id": self.client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "scope": "r_usr+w_usr+w_sub",
        }
        logger.debug("Checking with %s", data)
        resp = await self._api_post(f"{AUTH_URL}/token", data, auth=True)

        if "status" in resp and resp["status"] != 200:
            if resp["status"] == 400 and resp["sub_status"] == 1002:
                return 2, {}
            else:
                return 1, {}

        ret = {}
        ret["user_id"] = resp["user"]["userId"]
        ret["country_code"] = resp["user"]["countryCode"]
        ret["access_token"] = resp["access_token"]
        ret["refresh_token"] = resp["refresh_token"]
        ret["token_expiry"] = resp["expires_in"] + time.time()
        return 0, ret

    async def _refresh_access_token(self):
        """Refresh the access token given a refresh token.

        The access token expires in a week, so it must be refreshed.
        Requires a refresh token.
        """
        data = {
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = await self._api_post(f"{AUTH_URL}/token", data, auth=True)

        if resp.get("status", 200) != 200:
            # The refresh token itself has lapsed or been revoked, so there is
            # nothing left to refresh from and only a fresh device login will
            # help. Typed so the caller can offer that instead of dying with a
            # traceback.
            raise AuthenticationError(
                "Tidal refresh token has expired or been revoked."
            )

        c = self.config
        c.access_token = resp["access_token"]
        c.token_expiry = resp["expires_in"] + time.time()
        if resp.get("refresh_token"):
            c.refresh_token = resp["refresh_token"]
        # Persist the refreshed token, otherwise every later run refreshes again.
        f = self.global_config.file
        f.tidal.access_token = c.access_token
        f.tidal.token_expiry = c.token_expiry
        f.tidal.refresh_token = c.refresh_token
        f.set_modified()
        self._update_authorization_from_config()

    async def _get_device_code(self) -> tuple[str, str]:
        """Get the device code that will be used to log in on the browser."""
        if getattr(self, "session", None) is None or self.session.closed:
            self.session = await self.get_session(
                verify_ssl=self.global_config.session.downloads.verify_ssl
            )

        data = {
            "client_id": self.client_id,
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = await self._api_post(f"{AUTH_URL}/device_authorization", data)

        if resp.get("status", 200) != 200:
            raise Exception(f"Device authorization failed {resp}")

        return resp["deviceCode"], resp["verificationUriComplete"]

    # ---------- API Request Utilities ---------------

    async def _api_post(self, url, data, auth: bool = False) -> dict:
        """Post to the Tidal API. Status not checked!

        :param url:
        :param data:
        :param auth: send the client credentials (token endpoint)
        """
        headers = self.auth_headers if auth else None
        async with self.rate_limiter:
            async with self.session.post(url, data=data, headers=headers) as resp:
                return await resp.json()

    async def _api_request(self, path: str, params=None, base: str = BASE) -> dict:
        """Handle Tidal API requests.

        :param path:
        :type path: str
        :param params:
        :rtype: dict
        """
        if params is None:
            params = {}

        params["countryCode"] = self.config.country_code
        params.setdefault("limit", 100)

        async with self.rate_limiter:
            async with self.session.get(f"{base}/{path}", params=params) as resp:
                if resp.status == 404:
                    # Logged at debug, not warning: some callers ask for
                    # optional things (lyrics) where a 404 is the normal
                    # answer. Callers that do care log it themselves.
                    logger.debug("TIDAL: item not found (404): %s", resp.url)
                    raise ItemNotFoundError(f"TIDAL: item not found: {resp.url}")
                resp.raise_for_status()
                return await resp.json()
