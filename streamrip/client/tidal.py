import asyncio
import base64
import json
import logging
import random
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
from ..metadata.util import tidal_quality_id
from .client import Client, new_session
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
#   hi-res    24-bit FLAC (MPEG-DASH, up to 192 kHz) for hi-res releases, but
#             AAC 320 for ordinary lossless releases -- whatever is asked for
#
# So streamrip logs in with both (`hires_client`, on by default): each track is
# asked of the hi-res client first, and of the default one when it has no hi-res
# master. client_id/client_secret replace the default one (e.g. after Tidal
# revokes it).
DEFAULT_CLIENT_ID = _b64("NE4zbjZRMXg5NUxMNUs3cA==")
DEFAULT_CLIENT_SECRET = _b64(
    "b0tPWGZKVzM3MWNYNnhhWjBQeWhnR05CZE5MbEJaZDRBS0tZb3VnTWppaz0="
)
HIRES_CLIENT_ID = _b64("ZlgySnhkbW50WldLMGl4VA==")
HIRES_CLIENT_SECRET = _b64(
    "MU5tNUFmREFqeHJnSkZKYktOV0xlQXlLR1ZHbUlOdVhQUExIVlhBdnhBZz0="
)
QUALITY_MAP = {
    0: "LOW",  # AAC
    1: "HIGH",  # AAC
    2: "LOSSLESS",  # CD Quality
    3: "HI_RES",  # Best available: 24-bit FLAC (DASH) where the client may
}

# Tidal decides what it actually serves per track/client, independent of what
# was requested (e.g. no lossless master for that particular track), and
# says so in the response's own "audioQuality" -- not always the same string
# as what was requested (HI_RES_LOSSLESS in the response vs. HI_RES in the
# request).
_AUDIO_QUALITY_TIER = {
    "LOW": 0,
    "HIGH": 1,
    "LOSSLESS": 2,
    "HI_RES": 3,
    "HI_RES_LOSSLESS": 3,
}
LOSSLESS_TIER = _AUDIO_QUALITY_TIER["LOSSLESS"]
HIRES_TIER = _AUDIO_QUALITY_TIER["HI_RES"]
DASH_MIME = "application/dash+xml"

# Rate limiting and server errors are worth another attempt; other 4xx are not.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_API_ATTEMPTS = 4
MAX_RETRY_DELAY = 60
RATE_LIMIT_PAUSE = 10  # seconds per attempt when a 429 has no Retry-After


def _retry_after(resp, attempt: int) -> float:
    try:
        seconds = max(float(resp.headers["Retry-After"]), 1.0)
    except KeyError, ValueError:
        seconds = RATE_LIMIT_PAUSE * attempt
    return min(seconds, MAX_RETRY_DELAY)


def _dedup_duplicate_albums(albums: list[dict]) -> list[dict]:
    """Keep only the best copy when Tidal lists the same release twice.

    Tidal sometimes lists one album under an artist more than once: a clean
    and an explicit master, or just the same master at two quality tiers
    (a lossless entry and a separate hi-res one). Grouped by title and
    track count -- matching on both is as good as certain to be the same
    release, not two different albums that happen to share a title -- and
    the explicit, higher-quality copy is kept.
    """
    # Tidal's own catalog is inconsistent about which bracket style tags an
    # edition name, so square and round brackets are folded together before
    # grouping -- otherwise the same release under each style looks distinct.
    brackets = str.maketrans("[]", "()")

    groups: dict[tuple[str, int], list[dict]] = {}
    for album in albums:
        key = (
            (album.get("title") or "").strip().lower().translate(brackets),
            album.get("numberOfTracks", 0),
        )
        groups.setdefault(key, []).append(album)

    def best(album: dict) -> tuple[bool, int]:
        return (
            bool(album.get("explicit")),
            tidal_quality_id(album.get("audioQuality")),
        )

    return [max(group, key=best) for group in groups.values()]


class _Tokens:
    """One lane's login tokens, stored in [tidal] under an optional prefix."""

    FIELDS = ("access_token", "refresh_token", "token_expiry", "token_client_id")

    def __init__(self, section, prefix: str):
        object.__setattr__(self, "_section", section)
        object.__setattr__(self, "_prefix", prefix)

    def __getattr__(self, name):
        return getattr(self._section, self._prefix + name)

    def __setattr__(self, name, value):
        setattr(self._section, self._prefix + name, value)


class TidalClient(Client):
    """TidalClient."""

    source = "tidal"
    max_quality = 3
    # time.monotonic() before which no request is sent (set by a 429)
    _retry_at = 0.0

    def __init__(self, config: Config, hires_of: "TidalClient | None" = None):
        """Build a client lane; hires_of makes this the hi-res lane behind it."""
        self.logged_in = False
        self.global_config = config
        self.config = config.session.tidal
        c = self.config
        # Tidal serves each OAuth client other formats and no single one serves
        # them all (see above). With `hires_client` there are two lanes, each
        # with its own login: this one is asked for hi-res first, and the
        # lossless one when Tidal has no hi-res master for the track. The
        # hi-res lane keeps its tokens in the `hires_*` fields of [tidal].
        self._token_prefix = "hires_" if hires_of else ""
        self.tokens = _Tokens(c, self._token_prefix)
        self._root = hires_of or self
        if hires_of:
            self.rate_limiter = hires_of.rate_limiter
            self.client_id, self.client_secret = HIRES_CLIENT_ID, HIRES_CLIENT_SECRET
            self.hires_lane = None
        else:
            self.rate_limiter = self.get_rate_limiter(
                config.session.downloads.requests_per_minute,
            )
            if c.client_id and c.client_secret:
                self.client_id, self.client_secret = c.client_id, c.client_secret
            else:
                self.client_id = DEFAULT_CLIENT_ID
                self.client_secret = DEFAULT_CLIENT_SECRET
            # Only worth a second login when hi-res is what is asked for.
            wants_hires = c.hires_client and c.quality >= HIRES_TIER
            self.hires_lane = (
                TidalClient(config, hires_of=self) if wants_hires else None
            )
            # Tracks Tidal says have no hi-res master; see _note_hires_tags.
            self._no_hires: set[str] = set()
        # HTTP Basic auth for the token endpoint. Built by hand because
        # aiohttp.BasicAuth is deprecated as of aiohttp 3.14.
        credentials = f"{self.client_id}:{self.client_secret}".encode()
        self.auth_headers = {
            "Authorization": "Basic " + base64.b64encode(credentials).decode()
        }

    def lanes(self) -> list["TidalClient"]:
        """This client and, with `hires_client`, the hi-res one behind it."""
        return [self, self.hires_lane] if self.hires_lane else [self]

    async def close(self):
        """Close every lane's HTTP session."""
        for lane in self.lanes():
            if getattr(lane, "session", None) is not None:
                await lane.session.close()

    def save_login(self):
        """Copy this lane's tokens to the config file's copy, written on exit."""
        f = self.global_config.file
        saved = _Tokens(f.tidal, self._token_prefix)
        for name in _Tokens.FIELDS:
            setattr(saved, name, getattr(self.tokens, name))
        f.set_modified()

    async def login(self):
        """Log every lane in. Any lane missing or with a lapsed login raises."""
        for lane in self.lanes():
            await lane._login_lane()
        self.logged_in = True

    async def _login_lane(self):
        """Log this single lane in, refreshing its access token if it's stale."""
        if getattr(self, "session", None) is None or self.session.closed:
            self.session = new_session(
                verify_ssl=self.global_config.session.downloads.verify_ssl
            )
        c, t = self.config, self.tokens
        if not t.access_token:
            raise MissingCredentialsError(
                "No Tidal access token in config -- Tidal has not been set up yet."
            )
        if t.token_client_id != self.client_id:
            # Tokens are bound to the OAuth client that issued them. A token
            # from another client (an older streamrip, or before client_id was
            # changed in the config) keeps that client's format entitlements
            # and cannot be refreshed, so a fresh login is needed.
            raise MissingCredentialsError(
                "The saved Tidal login belongs to a different client id "
                "(streamrip now logs in once per format it can download, or "
                "the config changed the client). Please log in again."
            )

        try:
            self.token_expiry = float(t.token_expiry)
        except TypeError, ValueError:
            self.token_expiry = 0.0
        self.refresh_token = t.refresh_token

        if self.token_expiry - time.time() < 86400:  # 1 day
            await self._refresh_access_token()
        else:
            await self._login_by_access_token(t.access_token, c.user_id)

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
        if media_type == "track":
            self._note_hires_tags([item])
        if media_type in ("playlist", "album"):
            item["tracks"] = await self._get_tracks(url)
            if media_type == "album":
                await self._add_hires_format(item)
        elif media_type == "artist":
            logger.debug("filtering eps")
            album_resp, ep_resp = await asyncio.gather(
                self._api_request(f"{url}/albums"),
                self._api_request(f"{url}/albums", params={"filter": "EPSANDSINGLES"}),
            )

            item["albums"] = album_resp["items"]
            item["albums"].extend(ep_resp["items"])
            if self.global_config.session.metadata.prefer_explicit:
                item["albums"] = _dedup_duplicate_albums(item["albums"])
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

    async def _get_tracks(self, url: str) -> list[dict]:
        """The tracks of an album or playlist, fetched 100 at a time.

        Its items can include music videos, which can't be downloaded as
        tracks; they're left out rather than failing (and being retried by
        `streamrip repair`) one by one.
        """
        first = await self._api_request(f"{url}/items")
        pages = await asyncio.gather(
            *(
                self._api_request(f"{url}/items", {"offset": offset})
                for offset in range(100, first.get("totalNumberOfItems", 0), 100)
            )
        )
        items = [i for page in (first, *pages) for i in page["items"]]
        tracks = [i["item"] for i in items if i.get("type", "track") == "track"]
        if len(tracks) < len(items):
            logger.info(f"Skipping {len(items) - len(tracks)} video(s) in {url}")
        self._note_hires_tags(tracks)
        return tracks

    async def _add_hires_format(self, album: dict):
        """Say what a hi-res album really is, for its folder name.

        The album only reports LOSSLESS; the bit depth and sample rate are in a
        track's playback info. One extra request per hi-res album, and only a
        label, so any failure just leaves the default.
        """
        tags = (album.get("mediaMetadata") or {}).get("tags") or []
        first = next(
            (t for t in album["tracks"] if str(t["id"]) not in self._no_hires), None
        )
        if self.hires_lane is None or first is None or "HIRES_LOSSLESS" not in tags:
            return
        try:
            resp = await self.hires_lane._playback_info(str(first["id"]), HIRES_TIER)
        except Exception as e:
            logger.debug(f"Could not read the hi-res format of {album['id']}: {e}")
            return
        if _AUDIO_QUALITY_TIER.get(resp.get("audioQuality"), 0) >= HIRES_TIER:
            album["streamQuality"] = {
                "bitDepth": resp.get("bitDepth"),
                "sampleRate": resp.get("sampleRate"),
            }

    def _note_hires_tags(self, tracks: list[dict]):
        """Remember which tracks Tidal says have no hi-res master (like its own
        apps and tidal-dl-ng, go by the `HIRES_LOSSLESS` tag), so they never cost
        the hi-res client a request. Tracks without tag data are still asked.
        """
        for track in tracks:
            tags = (track.get("mediaMetadata") or {}).get("tags")
            if tags is not None and "HIRES_LOSSLESS" not in tags:
                self._no_hires.add(str(track["id"]))

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

    async def _playback_info(self, track_id: str, quality: int) -> dict:
        """Fetch a track's playback manifest at the given quality tier."""
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
        return resp

    async def _try_hires(self, track_id: str):
        """A hi-res downloadable, or None to ask the lossless client instead.

        The hi-res client is only ever served hi-res or AAC 320, so a track it
        has no hi-res master for -- or a failure of this optional attempt --
        goes to the lossless client, which serves FLAC and steps down from there.
        """
        try:
            resp = await self._playback_info(track_id, HIRES_TIER)
            tier = _AUDIO_QUALITY_TIER.get(resp.get("audioQuality"), 0)
            if tier >= HIRES_TIER and resp.get("manifestMimeType") == DASH_MIME:
                return self._get_downloadable_from_dash(track_id, resp)
            logger.debug(f"Track {track_id}: no hi-res master, using lossless")
        except NonStreamableError as e:
            logger.debug(f"Track {track_id}: no hi-res stream ({e}), using lossless")
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                # _api_request already logged and paused for this; not a
                # separate problem worth a second, scarier-looking warning.
                logger.debug(f"Track {track_id}: still rate limited, using lossless")
            else:
                logger.warning(
                    f"Track {track_id}: hi-res request failed ({e}); using lossless"
                )
        except Exception as e:
            logger.warning(
                f"Track {track_id}: hi-res request failed ({e}); using lossless"
            )
        return None

    async def get_downloadable(self, track_id: str, quality: int):
        """Return a downloadable for the track, trying the hi-res lane first."""
        quality = max(0, min(quality, self.max_quality))
        # Highest first: hi-res where Tidal has it, then whatever the lossless
        # client is served, one step down at a time.
        if (
            self.hires_lane is not None
            and quality >= HIRES_TIER
            and str(track_id) not in self._no_hires
        ):
            if (downloadable := await self.hires_lane._try_hires(track_id)) is not None:
                return downloadable

        resp = await self._playback_info(track_id, quality)

        actual_quality = resp.get("audioQuality")
        actual_tier = _AUDIO_QUALITY_TIER.get(actual_quality)
        if actual_tier is not None and actual_tier < min(quality, LOSSLESS_TIER):
            logger.warning(
                f"Track {track_id}: requested {QUALITY_MAP[quality]} but Tidal "
                f"only has {actual_quality} for it (most likely no lossless "
                "master for this specific track)."
            )
        elif actual_tier is not None and actual_tier < quality:
            # Quality 3 is "best available": LOSSLESS back for it is normal.
            logger.debug(f"Track {track_id}: no hi-res master, got {actual_quality}")

        # Hi-res (HI_RES_LOSSLESS) tracks are served as an MPEG-DASH manifest
        # rather than the usual base64 JSON; handle it instead of silently
        # falling back to a lower quality.
        if resp.get("manifestMimeType") == DASH_MIME:
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

    # ---------- Login Utilities ---------------

    async def _login_by_access_token(self, token: str, user_id: str):
        """Login using the access token.

        Used after the initial authorization.

        :param token: access token
        :param user_id: To verify that the user is correct
        """
        headers = {"authorization": f"Bearer {token}"}
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
        self.tokens.access_token = token
        self._update_authorization_from_config()

    def _update_authorization_from_config(self):
        """Point the session's Authorization header at the current access token."""
        self.session.headers.update(
            {"authorization": f"Bearer {self.tokens.access_token}"},
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

        t = self.tokens
        t.access_token = resp["access_token"]
        t.token_expiry = resp["expires_in"] + time.time()
        if resp.get("refresh_token"):
            t.refresh_token = resp["refresh_token"]
        # Persist the refreshed token, otherwise every later run refreshes again.
        self.save_login()
        self._update_authorization_from_config()

    async def _get_device_code(self) -> tuple[str, str]:
        """Get the device code that will be used to log in on the browser."""
        if getattr(self, "session", None) is None or self.session.closed:
            self.session = new_session(
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

        Connection errors, timeouts, 5xx and 429 are retried with backoff. A 429
        holds back every request, since the rate limit belongs to the account.

        :param path:
        :type path: str
        :param params:
        :rtype: dict
        """
        if params is None:
            params = {}

        params["countryCode"] = self.config.country_code
        params.setdefault("limit", 100)

        attempt = 0
        while True:
            attempt += 1
            if (pause := self._root._retry_at - time.monotonic()) > 0:
                # + jitter: every request blocked on the same 429 wakes at the
                # same instant otherwise, and immediately re-trips it together.
                await asyncio.sleep(pause + random.random() * 2)

            delay = 0.0
            try:
                async with self.rate_limiter:
                    async with self.session.get(
                        f"{base}/{path}", params=params
                    ) as resp:
                        if resp.status == 404:
                            # Logged at debug, not warning: some callers ask for
                            # optional things (lyrics) where a 404 is the normal
                            # answer. Callers that do care log it themselves.
                            logger.debug("TIDAL: item not found (404): %s", resp.url)
                            raise ItemNotFoundError(
                                f"TIDAL: item not found: {resp.url}"
                            )
                        if resp.status == 429:
                            self._pause_requests(_retry_after(resp, attempt))
                        if (
                            resp.status not in RETRY_STATUSES
                            or attempt == MAX_API_ATTEMPTS
                        ):
                            resp.raise_for_status()
                            return await resp.json()
                        if resp.status != 429:
                            reason = f"HTTP {resp.status}"
                            delay = 2**attempt + random.random()
            except aiohttp.ClientSSLError:
                raise
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
            ) as e:
                if attempt == MAX_API_ATTEMPTS:
                    raise
                reason = type(e).__name__
                delay = 2**attempt + random.random()

            if delay:
                logger.warning(
                    f"Tidal request failed ({reason}), retrying in {delay:.0f}s"
                )
                await asyncio.sleep(delay)

    def _pause_requests(self, seconds: float):
        """Block every lane's requests for seconds after a 429."""
        # The rate limit belongs to the account, so both lanes wait together.
        root = self._root
        now = time.monotonic()
        if root._retry_at <= now:
            logger.warning(
                f"Tidal is rate limiting us (HTTP 429); pausing for {seconds:.0f}s"
            )
        root._retry_at = max(root._retry_at, now + seconds)
