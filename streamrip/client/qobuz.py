import asyncio
import base64
import hashlib
import logging
import re
import time
from collections import OrderedDict
from urllib.parse import urlencode

from .. import USER_AGENT
from ..exceptions import (
    AccountBlockedError,
    AccountHalt,
    APIError,
    AuthenticationError,
    InvalidAppIdError,
    InvalidAppSecretError,
    MissingCredentialsError,
    NonStreamableError,
    restriction_message,
)
from .client import RETRY_STATUSES, Client, RequestClient, new_session
from .downloadable import BasicDownloadable, Downloadable
lazy from ..config import Config

BLOCKED = "Qobuz blocked this account; logging in again won't help."
LOGGED_OUT = "Qobuz no longer takes this login: log in to Qobuz again."

logger = logging.getLogger("streamrip")

QOBUZ_BASE_URL = "https://www.qobuz.com/api.json/0.2"
# Qobuz's own login page, the one its web player sends you to.
OAUTH_URL = "https://www.qobuz.com/signin/oauth"
# Qobuz's format_id for quality 1 to 4: MP3 320, FLAC 16/44.1, 24/96, 24/192.
FORMAT_IDS = (5, 6, 7, 27)


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
    return hashlib.md5(preimage.encode(), usedforsecurity=False).hexdigest()


# What the web player's bundle.js is scraped for: the app id, and secrets
# split over a seed per timezone plus that timezone's info and extras.
BUNDLE_URL_RE = re.compile(
    r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>'
)
APP_ID_RE = re.compile(r'production:{api:{appId:"(?P<app_id>\d{9})",appSecret:"\w{32}')
# What the web player trades the code of its login page with.
PRIVATE_KEY_RE = re.compile(r'privateKey:"(?P<key>\w+)"')
SEED_RE = re.compile(
    r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)'
)


def _wrong_signature(status: int, resp: dict) -> bool:
    """Qobuz's answer to a request signed with a wrong app secret."""
    return status == 400 and "signature" in str(resp.get("message", "")).lower()


class QobuzSpoofer(RequestClient):
    """Spoofs the information required to stream tracks from Qobuz."""

    source = "qobuz"
    user_agent = USER_AGENT  # streamrip says what it is to Qobuz

    def __init__(self, verify_ssl: bool = True, client: Client | None = None):
        """Create a Spoofer."""
        self.session = None
        self.verify_ssl = verify_ssl
        self.client = client
        self.private_key = ""
        self.rate_limiter = (
            client.rate_limiter if client is not None else self.get_rate_limiter(0)
        )

    async def get_app_id_and_secrets(self) -> tuple[str, list[str]]:
        """Scrape Qobuz's web player bundle for its app id and secrets."""
        login_page = await self._get_text_with_retries("https://play.qobuz.com/login")
        if (bundle_url := BUNDLE_URL_RE.search(login_page)) is None:
            raise InvalidAppIdError("Could not find Qobuz's web player bundle.")
        bundle = await self._get_text_with_retries(
            "https://play.qobuz.com" + bundle_url.group(1)
        )

        app_id = APP_ID_RE.search(bundle)
        if app_id is None:
            raise InvalidAppIdError("Could not find Qobuz's app id.")
        key = PRIVATE_KEY_RE.search(bundle)
        self.private_key = key.group("key") if key else ""

        secrets = OrderedDict(
            (m.group("timezone"), [m.group("seed")]) for m in SEED_RE.finditer(bundle)
        )
        if not secrets:
            raise InvalidAppSecretError("Could not find Qobuz's app secrets.")
        # The bundle picks between the first two seeds with ternaries that are
        # always false, so the second one is the one in use: try it first.
        if len(secrets) > 1:
            secrets.move_to_end(list(secrets)[1], last=False)

        timezones = "|".join(timezone.capitalize() for timezone in secrets)
        info_extras = re.compile(
            rf'name:"\w+/(?P<timezone>{timezones})",info:"(?P<info>[\w=]+)",'
            r'extras:"(?P<extras>[\w=]+)"'
        )
        for m in info_extras.finditer(bundle):
            timezone, info, extras = m.group("timezone", "info", "extras")
            secrets[timezone.lower()] += [info, extras]

        decoded = (
            base64.standard_b64decode("".join(parts)[:-44]).decode("utf-8")
            for parts in secrets.values()
        )
        return app_id.group("app_id"), [s for s in decoded if s]

    def _pause_owner(self) -> RequestClient:
        """Share the account pause with the API client during bootstrap."""
        return self.client._pause_owner() if self.client is not None else self

    async def __aenter__(self):
        """Open the spoofer's own HTTP session."""
        self.session = new_session(
            verify_ssl=self.verify_ssl, user_agent=self.user_agent
        )
        return self

    async def __aexit__(self, *_):
        """Close the spoofer's HTTP session."""
        if self.session is not None:
            await self.session.close()
        self.session = None


class QobuzClient(Client):
    """Qobuz: a user id and token, and the app id and secret of its web player."""

    source = "qobuz"
    max_quality = 4
    user_agent = USER_AGENT  # streamrip says what it is to Qobuz

    def __init__(self, config: Config):
        super().__init__(config)
        self.secret: str | None = None
        # The app id and the login, sent with each API request. Not put on the
        # session: the session also fetches the files, covers and booklets,
        # from whatever address Qobuz's answers name, and those get no login.
        self._login_headers: dict[str, str] = {}
        # True for a free account (no streaming subscription) that can still
        # download content it has *purchased* from the Qobuz download store.
        # When set, file-url requests use intent=download instead of stream.
        self.download_only: bool = False
        # Held while a track finds the app secret that works (see _file_url).
        self._finding_secret = asyncio.Lock()
        # The web player's key for its login page's code (oauth_link).
        self._private_key = ""

    async def login(self):
        """Log in with the user id and user_auth_token saved in the config."""
        self._ensure_session()
        try:
            await self._login()
        except BaseException:
            # Close the session so a failed login does not leave an unclosed
            # connector behind (and a re-login attempt starts from scratch).
            await self.session.close()
            raise

    async def _login(self):
        """Log in, fetching Qobuz's app id/secret first if not cached yet."""
        c = self.config
        if not c.user_id or not c.auth_token:
            raise MissingCredentialsError

        if not c.app_id or not c.secrets:
            logger.info(
                "Fetching Qobuz's app id and secret (a one-time setup step, "
                "not your login -- cached in the config afterward)"
            )
            await self._refresh_app_id_and_secrets()

        # A stale app id (Qobuz changed it) fails with InvalidAppIdError:
        # fetch the bundle's again and retry once. Stale secrets show only at
        # a file URL (_file_url).
        try:
            await self._attempt_login()
        except InvalidAppIdError as e:
            logger.warning("Login failed with %s, refetching app id/secrets", e)
            self._login_headers.pop("X-User-Auth-Token", None)
            await self._refresh_app_id_and_secrets()
            await self._attempt_login()

        self.logged_in = True

    async def oauth_link(self, redirect: str) -> str:
        """Qobuz's own login page, which sends the browser to `redirect` with a
        code: the web player's login, with its app id."""
        await self._refresh_app_id_and_secrets()
        query = urlencode({"ext_app_id": self.config.app_id, "redirect_url": redirect})
        return f"{OAUTH_URL}?{query}"

    async def oauth_token(self, code: str) -> tuple[str, str]:
        """The user id and token for the code the login page sent back."""
        self._ensure_session()
        self._login_headers["X-App-Id"] = str(self.config.app_id)
        status, resp = await self._api_request(
            "oauth/callback", {"code": code, "private_key": self._private_key}
        )
        if status != 200 or not resp.get("token") or not resp.get("user_id"):
            raise AuthenticationError(
                f"Qobuz did not take the login (HTTP {status}): log in again."
            )
        return str(resp["user_id"]), str(resp["token"])

    async def _refresh_app_id_and_secrets(self):
        c = self.config
        c.app_id, c.secrets = await self._get_app_id_and_secrets()
        # In this session, and in the config file written on exit.
        f = self.global_config.file
        f.qobuz.app_id = c.app_id
        f.qobuz.secrets = c.secrets
        f.set_modified()

    async def _attempt_login(self):
        c = self.config
        self._login_headers["X-App-Id"] = str(c.app_id)
        # As the web player logs in: the token in a header, not in the address.
        self._login_headers["X-User-Auth-Token"] = c.auth_token
        status, resp = await self._api_request("user/login", {}, "extra=partner")
        # The response carries the user_auth_token and the account profile.
        logger.debug("Login response keys: %s", sorted(resp))

        if status == 401:
            raise AuthenticationError(
                "Invalid Qobuz user id or user_auth_token. The token may have "
                "expired; log in again to get a fresh one."
            )
        if status == 400:
            raise InvalidAppIdError(f"Qobuz rejected app id {c.app_id}")
        if status != 200:
            raise APIError(
                f"Qobuz login failed (HTTP {status}): "
                f"{resp.get('message') or 'no message'}"
            )

        logger.debug("Logged in to Qobuz")

        # An empty credential.parameters means the account has no active
        # streaming subscription. Such a (free) account cannot stream, but it
        # CAN still download albums it has purchased from the Qobuz download
        # store, so flag the client as download-only instead of refusing.
        self._credential = resp["user"]["credential"]
        if not resp["user"]["credential"]["parameters"]:
            self.download_only = True
            logger.warning(
                "Free Qobuz account detected (no streaming subscription): only "
                "purchased download-store content can be downloaded."
            )

        self._login_headers["X-User-Auth-Token"] = resp["user_auth_token"]

        # The one that worked last time: _file_url keeps it first.
        self.secret = c.secrets[0]
        await self._fit_quality()

    async def account_plan(self) -> tuple[str, int | None]:
        credential = getattr(self, "_credential", None) or {}
        flags = credential.get("parameters") or {}
        if flags.get("hires_streaming"):
            kind, best = "hi-res", 4
        elif flags.get("lossless_streaming"):
            kind, best = "lossless", 2
        elif flags.get("lossy_streaming"):
            kind, best = "mp3 only", 1
        else:
            return "downloads of purchases only", None
        label = credential.get("label")
        return f"{label}, {kind}" if label else kind, best

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        if str(item_id).startswith("favorites:"):
            return await self._favorites(item_id.removeprefix("favorites:"))
        if media_type in {"artist", "label"}:
            return await self._with_all_albums(media_type, item_id)

        params = {
            "app_id": str(self.config.app_id),
            f"{media_type}_id": item_id,
            # Do these matter?
            "limit": 500,
            "offset": 0,
        }

        extras = {
            # tracks may come back empty (July 2026 API change); track_ids is
            # the replacement. Request both so either response shape works.
            "playlist": "tracks,track_ids",
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

    async def _favorites(self, kind: str) -> dict:
        """The account's favorite tracks as a playlist, or its favorite albums
        as a label."""
        epoint, params = "favorite/getUserFavorites", {"type": kind, "limit": 500}
        first = await self._request_ok(epoint, params)
        pages = await asyncio.gather(
            *(
                self._request_ok(epoint, {**params, "offset": offset})
                for offset in range(500, first[kind]["total"], 500)
            )
        )
        items = [item for page in (first, *pages) for item in page[kind]["items"]]
        if kind == "albums":
            return {"name": "Qobuz favorite albums", "albums": {"items": items}}
        return {"name": "Qobuz favorites", "tracks": {"items": items}}

    async def _with_all_albums(self, media_type: str, item_id: str) -> dict:
        """An artist or label with its whole catalog, fetched 500 albums at a time."""
        epoint = f"{media_type}/get"
        params = {
            "app_id": str(self.config.app_id),
            f"{media_type}_id": item_id,
            "limit": 500,
            "offset": 0,
            "extra": "albums",
        }
        resp = await self._request_ok(epoint, params)
        pages = await asyncio.gather(
            *(
                self._request_ok(epoint, {**params, "offset": offset})
                for offset in range(500, resp.get("albums_count", 0), 500)
            )
        )
        for page in pages:
            resp["albums"]["items"].extend(page["albums"]["items"])
        return resp

    async def search(self, media_type: str, query: str, limit: int = 500) -> list[dict]:
        """Search response pages, bounded by the caller's result limit."""
        self.check_search_type(media_type)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("Qobuz search limit must be a non-negative integer")
        # Config values are tomlkit Integer subclasses. Normalize before using
        # the limit as a fallback for strictly validated API pagination metadata.
        limit = int(limit)
        if limit == 0:
            return []

        epoint = f"{media_type}/search"
        params = {"query": query, "limit": limit, "offset": 0}
        page = await self._request_ok(epoint, params)
        logger.debug("paginate: initial request succeeded")
        items = page.get(f"{media_type}s", {})
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
        return [page] + [
            await self._request_ok(
                epoint,
                {**params, "offset": offset, "limit": min(page_size, total - offset)},
            )
            for offset in range(page_size, total, page_size)
        ]

    async def get_downloadable(self, item_id: str, quality: int) -> Downloadable:
        """Ask Qobuz for a track's file URL at `quality` (1 to 4)."""
        if self.secret is None or not self.logged_in:
            raise RuntimeError("Qobuz is not logged in")
        # Qobuz has no quality 0 (128 kbps); clamp instead of asserting so
        # `streamrip --quality 0` still works for mixed-source downloads.
        quality = max(1, min(quality, self.max_quality))
        try:
            status, resp_json = await self._file_url(item_id, quality)
        except AccountBlockedError as e:
            raise AccountHalt(str(e)) from e  # every next track would get the same
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
                        item_id,
                        quality - 1,
                    )
                    return await self.get_downloadable(item_id, quality - 1)
                raise NonStreamableError(restriction_message(code) + ".")
            raise NonStreamableError

        if resp_json.get("sample"):  # 30 seconds, for an account without the rights
            raise NonStreamableError(
                "Qobuz only gives a 30-second preview of this track."
            )
        # Qobuz serves the best it has up to the asked format: 24/96 for a
        # quality 4 request of an album without 24/192, an MP3 (5) for no FLAC.
        served = resp_json.get("format_id")
        downloadable = BasicDownloadable(
            self.session,
            stream_url,
            "mp3" if served == 5 or (served is None and quality == 1) else "flac",
            source="qobuz",
        )
        downloadable.quality = (
            FORMAT_IDS.index(served) + 1 if served in FORMAT_IDS else quality
        )
        return downloadable

    async def _request_ok(self, epoint: str, params: dict) -> dict:
        """_api_request that insists on HTTP 200, asking once more after a blip:
        a 5xx it leaves alone, or the 400 of Qobuz's search ("...check your
        Algolia Application Id.") that works moments later."""
        for attempt in (1, 2):
            status, page = await self._api_request(epoint, params)
            if status == 200:
                return page
            message = (page.get("message") if isinstance(page, dict) else None) or ""
            transient = (
                status >= 500 and status not in RETRY_STATUSES
            ) or "Algolia" in message
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

    async def _get_app_id_and_secrets(self) -> tuple[str, list[str]]:
        async with QobuzSpoofer(
            verify_ssl=self.global_config.session.downloads.verify_ssl, client=self
        ) as spoofer:
            found = await spoofer.get_app_id_and_secrets()
            self._private_key = spoofer.private_key
            return found

    async def _file_url(self, track_id: str, quality: int) -> tuple[int, dict]:
        """track/getFileUrl, signed with the app secret that worked last.

        One of the web player's secrets works. The others are only tried when
        Qobuz calls the signature wrong, never just to test them, and the one
        that works is kept first for the next run.
        """
        first = self.secret
        status, resp = await self._request_file_url(track_id, quality, first)
        if not _wrong_signature(status, resp):
            return status, resp
        async with self._finding_secret:  # one track finds it, the rest use it
            tried = {first}
            for fetch in (False, True):
                if fetch:  # Qobuz changed them: the web player's own again
                    await self._refresh_app_id_and_secrets()
                    await self._attempt_login()
                for secret in self.config.secrets:
                    if secret in tried:
                        continue
                    tried.add(secret)
                    status, resp = await self._request_file_url(
                        track_id, quality, secret
                    )
                    if not _wrong_signature(status, resp):
                        self._keep_secret(secret)
                        return status, resp
        # Every next track would ask in vain too.
        raise AccountHalt("Qobuz took none of its web player's app secrets.")

    def _keep_secret(self, secret: str):
        """Sign with `secret` from now on: first in the config, for the next run."""
        c = self.config
        c.secrets = [secret, *(s for s in c.secrets if s != secret)]
        self.global_config.file.qobuz.secrets = c.secrets
        self.global_config.file.set_modified()
        self.secret = secret

    async def _request_file_url(
        self,
        track_id: str,
        quality: int,
        secret: str,
    ) -> tuple[int, dict]:
        """Ask for a track's file URL at quality 1 to 4: (status, JSON body)."""
        format_id = FORMAT_IDS[quality - 1]
        unix_ts = time.time()
        # Owned-only (free) accounts must request intent=download; streaming
        # accounts use intent=stream. The signed preimage and the params dict
        # MUST agree on the value or Qobuz rejects the request with HTTP 400.
        intent = "download" if self.download_only else "stream"
        params = {
            "request_ts": unix_ts,
            "request_sig": file_url_signature(
                format_id, intent, track_id, unix_ts, secret
            ),
            "track_id": track_id,
            "format_id": format_id,
            "intent": intent,
        }
        status, resp = await self._api_request("track/getFileUrl", params)
        # The signature was right (a wrong secret is a 400): it is the account.
        if status == 403 and "blocked" in str(resp.get("message", "")).lower():
            raise AccountBlockedError(BLOCKED)
        return status, resp

    async def _api_request(
        self, epoint: str, params: dict, data: str | None = None
    ) -> tuple[int, dict]:
        """GET a Qobuz API endpoint (POST `data`): (status, JSON body), whatever
        the status."""
        # Only the endpoint: params can carry the request signature.
        logger.debug("api_request: endpoint=%s", epoint)
        status, resp = await self._get_json(
            f"{QOBUZ_BASE_URL}/{epoint}", params, self._login_headers, data
        )
        # The login is gone, and every next request would hear the same. (A
        # file URL's 401 can be the plan's: this track may not be streamed.)
        if status == 401 and self.logged_in and epoint != "track/getFileUrl":
            raise AccountHalt(LOGGED_OUT)
        return status, resp
