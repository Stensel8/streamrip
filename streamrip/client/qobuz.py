import asyncio
import base64
import hashlib
import logging
import re
import time
from collections import OrderedDict
from typing import List, Optional

from ..config import Config
from ..exceptions import (
    APIError,
    AuthenticationError,
    InvalidAppIdError,
    InvalidAppSecretError,
    MissingCredentialsError,
    NonStreamableError,
)
from .client import Client, new_session
from .downloadable import BasicDownloadable, Downloadable

logger = logging.getLogger("streamrip")

QOBUZ_BASE_URL = "https://www.qobuz.com/api.json/0.2"


def file_url_signature(
    format_id: int, intent: str, track_id: str, timestamp: float, secret: str
) -> str:
    """The request_sig Qobuz requires on track/getFileUrl.

    MD5 is dictated by the Qobuz API: this is a request signature, not a way
    of storing a secret, and Qobuz rejects anything else.
    """
    preimage = (
        f"trackgetFileUrlformat_id{format_id}intent{intent}"
        f"track_id{track_id}{timestamp}{secret}"
    )
    # codeql[py/weak-sensitive-data-hashing] Qobuz's API mandates MD5 here.
    return hashlib.md5(preimage.encode("utf-8")).hexdigest()


class QobuzSpoofer:
    """Spoofs the information required to stream tracks from Qobuz."""

    def __init__(self, verify_ssl: bool = True):
        """Create a Spoofer."""
        self.seed_timezone_regex = (
            r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.ut'
            r"imezone\.(?P<timezone>[a-z]+)\)"
        )
        # note: {timezones} should be replaced with every capitalized timezone joined by a |
        self.info_extras_regex = (
            r'name:"\w+/(?P<timezone>{timezones})",info:"'
            r'(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'
        )
        self.app_id_regex = (
            r'production:{api:{appId:"(?P<app_id>\d{9})",appSecret:"(\w{32})'
        )
        self.session = None
        self.verify_ssl = verify_ssl

    async def get_app_id_and_secrets(self) -> tuple[str, list[str]]:
        """Scrape Qobuz's web player bundle for its app id and secrets."""
        assert self.session is not None
        async with self.session.get("https://play.qobuz.com/login") as req:
            login_page = await req.text()

        bundle_url_match = re.search(
            r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>',
            login_page,
        )
        assert bundle_url_match is not None
        bundle_url = bundle_url_match.group(1)

        async with self.session.get("https://play.qobuz.com" + bundle_url) as req:
            self.bundle = await req.text()

        match = re.search(self.app_id_regex, self.bundle)
        if match is None:
            raise Exception("Could not find app id.")

        app_id = str(match.group("app_id"))

        # get secrets
        seed_matches = re.finditer(self.seed_timezone_regex, self.bundle)
        secrets = OrderedDict()
        for match in seed_matches:
            seed, timezone = match.group("seed", "timezone")
            secrets[timezone] = [seed]

        """
        The code that follows switches around the first and second timezone.
        Qobuz uses two ternary (a shortened if statement) conditions that
        should always return false. The way Javascript's ternary syntax
        works, the second option listed is what runs if the condition returns
        false. Because of this, we must prioritize the *second* seed/timezone
        pair captured, not the first.
        """

        keypairs = list(secrets.items())
        secrets.move_to_end(keypairs[1][0], last=False)

        info_extras_regex = self.info_extras_regex.format(
            timezones="|".join(timezone.capitalize() for timezone in secrets),
        )
        info_extras_matches = re.finditer(info_extras_regex, self.bundle)
        for match in info_extras_matches:
            timezone, info, extras = match.group("timezone", "info", "extras")
            secrets[timezone.lower()] += [info, extras]

        for secret_pair in secrets:
            secrets[secret_pair] = base64.standard_b64decode(
                "".join(secrets[secret_pair])[:-44],
            ).decode("utf-8")

        vals: List[str] = list(secrets.values())
        if "" in vals:
            vals.remove("")

        secrets_list = vals

        return app_id, secrets_list

    async def __aenter__(self):
        """Open the spoofer's own HTTP session."""
        self.session = new_session(verify_ssl=self.verify_ssl)
        return self

    async def __aexit__(self, *_):
        """Close the spoofer's HTTP session."""
        if self.session is not None:
            await self.session.close()
        self.session = None


class QobuzClient(Client):
    source = "qobuz"
    max_quality = 4

    def __init__(self, config: Config):
        self.logged_in = False
        self.config = config
        self.rate_limiter = self.get_rate_limiter(
            config.session.downloads.requests_per_minute,
        )
        self.secret: Optional[str] = None
        # True for a free account (no streaming subscription) that can still
        # download content it has *purchased* from the Qobuz download store.
        # When set, file-url requests use intent=download instead of stream.
        self.download_only: bool = False

    async def login(self):
        """Log in with the user id and user_auth_token saved in the config."""
        self.session = new_session(verify_ssl=self.config.session.downloads.verify_ssl)
        try:
            await self._login()
        except BaseException:
            # Close the session so a failed login does not leave an unclosed
            # connector behind (and a re-login attempt starts from scratch).
            await self.session.close()
            raise

    async def _login(self):
        """Log in, fetching Qobuz's app id/secret first if not cached yet."""
        c = self.config.session.qobuz
        if not c.user_id or not c.auth_token:
            raise MissingCredentialsError

        assert not self.logged_in, "Already logged in"

        if not c.app_id or not c.secrets:
            logger.info(
                "Fetching Qobuz's app id and secret (a one-time setup step, "
                "not your login -- cached in the config afterward)"
            )
            await self._refresh_app_id_and_secrets()

        # A stale app_id/secret pair (e.g. hardcoded in the config after Qobuz
        # rotated its app secret) fails with InvalidAppIdError or
        # InvalidAppSecretError. Re-fetch a fresh pair from the bundle and retry
        # once before giving up.
        try:
            await self._attempt_login()
        except (InvalidAppIdError, InvalidAppSecretError) as e:
            logger.warning("Login failed with %s, refetching app id/secrets", e)
            self.session.headers.pop("X-User-Auth-Token", None)
            await self._refresh_app_id_and_secrets()
            await self._attempt_login()

        self.logged_in = True

    async def _refresh_app_id_and_secrets(self):
        c = self.config.session.qobuz
        c.app_id, c.secrets = await self._get_app_id_and_secrets()
        # write to file
        f = self.config.file
        f.qobuz.app_id = c.app_id
        f.qobuz.secrets = c.secrets
        f.set_modified()

    async def _attempt_login(self):
        c = self.config.session.qobuz
        self.session.headers.update({"X-App-Id": str(c.app_id)})

        params = {
            "user_id": c.user_id,
            "user_auth_token": c.auth_token,
            "app_id": str(c.app_id),
        }
        status, resp = await self._api_request("user/login", params)
        # The response carries the user_auth_token and the account profile.
        logger.debug("Login response keys: %s", sorted(resp))

        if status == 401:
            raise AuthenticationError(
                "Invalid Qobuz user id or user_auth_token. The token may have "
                "expired; log in again to get a fresh one."
            )
        elif status == 400:
            raise InvalidAppIdError(f"Qobuz rejected app id {c.app_id}")
        elif status != 200:
            raise APIError(
                f"Qobuz login failed (HTTP {status}): "
                f"{resp.get('message') or 'no message'}"
            )

        logger.debug("Logged in to Qobuz")

        # An empty credential.parameters means the account has no active
        # streaming subscription. Such a (free) account cannot stream, but it
        # CAN still download albums it has purchased from the Qobuz download
        # store, so flag the client as download-only instead of refusing.
        if not resp["user"]["credential"]["parameters"]:
            self.download_only = True
            logger.warning(
                "Free Qobuz account detected (no streaming subscription): only "
                "purchased download-store content can be downloaded."
            )

        uat = resp["user_auth_token"]
        self.session.headers.update({"X-User-Auth-Token": uat})

        self.secret = await self._get_valid_secret(c.secrets)

    async def get_metadata(self, item: str, media_type: str):
        if media_type == "label":
            return await self.get_label(item)

        c = self.config.session.qobuz
        params = {
            "app_id": str(c.app_id),
            f"{media_type}_id": item,
            # Do these matter?
            "limit": 500,
            "offset": 0,
        }

        extras = {
            "artist": "albums",
            # tracks may come back empty (July 2026 API change); track_ids is
            # the replacement. Request both so either response shape works.
            "playlist": "tracks,track_ids",
            "label": "albums",
            # Qobuz's album/get stopped inlining "tracks" (July 2026); request
            # the id list instead. Consumed by get_album_track_ids().
            "album": "track_ids",
        }

        if media_type in extras:
            params.update({"extra": extras[media_type]})

        logger.debug("request params: %s", params)

        epoint = f"{media_type}/get"

        status, resp = await self._api_request(epoint, params)

        if status != 200:
            raise NonStreamableError(
                f'Error fetching metadata. Message: "{resp.get("message")}"',
            )

        if media_type == "playlist":
            await self._fetch_remaining_playlist_tracks(epoint, params, resp)

        return resp

    async def _fetch_remaining_playlist_tracks(
        self, epoint: str, params: dict, resp: dict
    ):
        """Page through playlists longer than one response (500 tracks).

        Only needed when Qobuz did not return the complete ``track_ids`` list,
        which PlaylistMetadata prefers when present.
        """
        if resp.get("track_ids"):
            return
        tracks = resp.get("tracks") or {}
        items = tracks.get("items") or []
        total = int(tracks.get("total") or resp.get("tracks_count") or 0)
        if not items or total <= len(items):
            return

        limit = int(params.get("limit", 500))
        pages = await asyncio.gather(
            *[
                self._request_ok(epoint, {**params, "offset": offset})
                for offset in range(len(items), total, limit)
            ]
        )
        for page in pages:
            items.extend((page.get("tracks") or {}).get("items") or [])
        logger.debug("Fetched %d/%d playlist tracks", len(items), total)

    async def get_label(self, label_id: str) -> dict:
        c = self.config.session.qobuz
        page_limit = 500
        params = {
            "app_id": str(c.app_id),
            "label_id": label_id,
            "limit": page_limit,
            "offset": 0,
            "extra": "albums",
        }
        epoint = "label/get"
        status, label_resp = await self._api_request(epoint, params)
        assert status == 200
        albums_count = label_resp["albums_count"]

        if albums_count <= page_limit:
            return label_resp

        requests = [
            self._api_request(
                epoint,
                {
                    "app_id": str(c.app_id),
                    "label_id": label_id,
                    "limit": page_limit,
                    "offset": offset,
                    "extra": "albums",
                },
            )
            for offset in range(page_limit, albums_count, page_limit)
        ]

        results = await asyncio.gather(*requests)
        items = label_resp["albums"]["items"]
        for status, resp in results:
            assert status == 200
            items.extend(resp["albums"]["items"])

        return label_resp

    async def search(self, media_type: str, query: str, limit: int = 500) -> list[dict]:
        if media_type not in ("artist", "album", "track", "playlist"):
            raise Exception(f"{media_type} not available for search on qobuz")

        params = {
            "query": query,
        }
        epoint = f"{media_type}/search"

        return await self._paginate(epoint, params, limit=limit)

    async def get_downloadable(self, item: str, quality: int) -> Downloadable:
        assert self.secret is not None and self.logged_in
        # Qobuz has no quality 0 (128 kbps); clamp instead of asserting so
        # `streamrip --quality 0` still works for mixed-source downloads.
        quality = max(1, min(quality, self.max_quality))
        status, resp_json = await self._request_file_url(item, quality, self.secret)
        if status != 200:
            raise NonStreamableError(
                f"Could not get a download URL (HTTP {status}): "
                f"{resp_json.get('message') or 'no message'}"
            )
        stream_url = resp_json.get("url")

        if stream_url is None:
            restrictions = resp_json.get("restrictions")
            if restrictions:
                code = restrictions[0]["code"]
                # Purchased (download-only) content is sold in exactly one
                # format and Qobuz offers no automatic fallback: requesting a
                # higher tier than the purchased one fails with
                # FormatRestrictedByFormatAvailability. Step down one tier.
                if (
                    self.download_only
                    and quality > 1
                    and code == "FormatRestrictedByFormatAvailability"
                ):
                    logger.warning(
                        "Quality %d unavailable for purchased track %s; "
                        "retrying at quality %d.",
                        quality,
                        item,
                        quality - 1,
                    )
                    return await self.get_downloadable(item, quality - 1)
                # Turn CamelCase code into a readable sentence
                words = re.findall(r"([A-Z][a-z]+)", code)
                raise NonStreamableError(
                    words[0] + " " + " ".join(map(str.lower, words[1:])) + ".",
                )
            raise NonStreamableError

        return BasicDownloadable(
            self.session, stream_url, "flac" if quality > 1 else "mp3", source="qobuz"
        )

    async def _request_ok(self, epoint: str, params: dict) -> dict:
        """_api_request that insists on HTTP 200, retrying once if search blips.

        Qobuz's search backend fails intermittently -- a 400 reading
        "Impossible to connect, please check your Algolia Application Id."
        that succeeds moments later. A 400 isn't something _api_request
        retries (5xx already are), so one short retry here absorbs it;
        anything else is raised with Qobuz's own message rather than a bare
        AssertionError.
        """
        for attempt in (1, 2):
            status, page = await self._api_request(epoint, params)
            if status == 200:
                return page
            message = (page.get("message") if isinstance(page, dict) else None) or ""
            transient = "Algolia" in message
            if attempt == 1 and transient:
                logger.warning(
                    "Qobuz %s failed (HTTP %d: %s) -- retrying once",
                    epoint,
                    status,
                    message or "no message",
                )
                await asyncio.sleep(3)
                continue
            break
        raise APIError(
            f"Qobuz {epoint} failed (HTTP {status}): {message or 'no message'}"
        )

    async def _paginate(
        self,
        epoint: str,
        params: dict,
        limit: int = 500,
    ) -> list[dict]:
        """Return search response pages, bounded by the caller's result limit."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("Qobuz search limit must be a non-negative integer")
        # Config values are tomlkit Integer subclasses. Normalize before using
        # the limit as a fallback for strictly validated API pagination metadata.
        limit = int(limit)
        if limit == 0:
            return []

        params = {**params, "limit": limit, "offset": 0}
        page = await self._request_ok(epoint, params)
        logger.debug("paginate: initial request succeeded")
        # albums, tracks, etc.
        key = epoint.split("/")[0] + "s"
        items = page.get(key, {})
        total = items.get("total", 0)
        if type(total) is not int or total < 0:
            raise APIError("Qobuz search returned an invalid pagination total")
        total = min(total, limit)

        logger.debug("paginate: %d total items requested", total)

        if total == 0:
            logger.debug("Nothing found from %s epoint", epoint)
            return []

        page_size = items.get("limit", min(500, limit))
        if type(page_size) is not int or not 0 < page_size <= limit:
            raise APIError("Qobuz search returned an invalid pagination limit")

        # Qobuz may cap the requested page size. Keep that validated size,
        # but never use response offsets to control progress or allocate a
        # batch of requests from response metadata.
        pages = [page]
        for offset in range(page_size, total, page_size):
            pages.append(
                await self._request_ok(
                    epoint,
                    {
                        **params,
                        "offset": offset,
                        "limit": min(page_size, total - offset),
                    },
                )
            )
        return pages

    async def _get_app_id_and_secrets(self) -> tuple[str, list[str]]:
        async with QobuzSpoofer(
            verify_ssl=self.config.session.downloads.verify_ssl
        ) as spoofer:
            return await spoofer.get_app_id_and_secrets()

    async def _test_secret(self, secret: str) -> Optional[str]:
        status, _ = await self._request_file_url("19512574", 4, secret)
        if status == 400:
            return None
        if status == 200 or status == 401:
            return secret
        logger.warning("Got status %d when testing secret", status)
        return None

    async def _get_valid_secret(self, secrets: list[str]) -> str:
        results = await asyncio.gather(
            *[self._test_secret(secret) for secret in secrets],
        )
        working_secrets = [r for r in results if r is not None]
        if len(working_secrets) == 0:
            raise InvalidAppSecretError(secrets)

        return working_secrets[0]

    async def _request_file_url(
        self,
        track_id: str,
        quality: int,
        secret: str,
    ) -> tuple[int, dict]:
        quality = self.get_quality(quality)
        unix_ts = time.time()
        # Owned-only (free) accounts must request intent=download; streaming
        # accounts use intent=stream. The signed preimage and the params dict
        # MUST agree on the value or Qobuz rejects the request with HTTP 400.
        intent = "download" if self.download_only else "stream"
        params = {
            "request_ts": unix_ts,
            "request_sig": file_url_signature(
                quality, intent, track_id, unix_ts, secret
            ),
            "track_id": track_id,
            "format_id": quality,
            "intent": intent,
        }
        return await self._api_request("track/getFileUrl", params)

    async def _api_request(self, epoint: str, params: dict) -> tuple[int, dict]:
        """Make a request to the API.
        returns: status code, json parsed response
        """
        # Only the endpoint: params carry credentials (user_auth_token)
        # and the request signature.
        logger.debug("api_request: endpoint=%s", epoint)

        async def read(response) -> tuple[int, dict]:
            if "json" not in (response.content_type or ""):
                # An HTML error page, such as a 502 from Qobuz's edge.
                # aiohttp's ContentTypeError would quote the full request
                # URL, which carries user_auth_token -- so report the
                # status instead of letting that propagate.
                return response.status, {
                    "message": f"non-JSON response ({response.content_type})"
                }
            return response.status, await response.json()

        return await self._get_with_retries(
            f"{QOBUZ_BASE_URL}/{epoint}", read, params=params
        )

    @staticmethod
    def get_quality(quality: int):
        quality_map = (5, 6, 7, 27)
        return quality_map[quality - 1]
