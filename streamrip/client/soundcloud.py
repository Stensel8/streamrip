import asyncio
import itertools
import logging
import random
import re

from ..config import Config
from ..exceptions import NonStreamableError
from .client import Client, new_session
from .downloadable import SoundcloudDownloadable

# e.g. 123456-293847-121314-209849
USER_ID = "-".join(str(random.randint(111111, 999999)) for _ in range(4))
BASE = "https://api-v2.soundcloud.com"
STOCK_URL = "https://soundcloud.com/"

# for playlists
MAX_BATCH_SIZE = 50

logger = logging.getLogger("streamrip")


class SoundcloudClient(Client):
    source = "soundcloud"
    logged_in = False

    NON_STREAMABLE = "_non_streamable"
    ORIGINAL_DOWNLOAD = "_original_download"
    NOT_RESOLVED = "_not_resolved"

    def __init__(self, config: Config):
        self.global_config = config
        self.config = config.session.soundcloud
        self.rate_limiter = self.get_rate_limiter(
            config.session.downloads.requests_per_minute,
        )

    async def login(self):
        self.session = new_session(
            verify_ssl=self.global_config.session.downloads.verify_ssl
        )
        client_id, app_version = self.config.client_id, self.config.app_version
        # app_version is optional: SoundCloud does not always expose it, and
        # requiring it made every run re-scrape the website.
        if not client_id or not (await self._announce_success()):
            client_id, app_version = await self._refresh_tokens()
            # In this session, and in the config file written on exit.
            for c in (self.config, self.global_config.file.soundcloud):
                c.client_id, c.app_version = client_id, app_version
            self.global_config.file.set_modified()

        logger.debug(f"Current valid {client_id=} {app_version=}")
        self.logged_in = True

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        """Fetch metadata for an item in Soundcloud API.

        Args:

            item_id (str): Plain soundcloud item ID (e.g 1633786176)
            media_type (str): track or playlist

        Returns:

            API response. The item IDs for the tracks in the playlist are modified to
            include resolution status.
        """
        if media_type == "track":
            # parse custom id that we injected
            _item_id, _ = item_id.split("|")
            return await self._get_track(_item_id)
        elif media_type == "playlist":
            return await self._get_playlist(item_id)
        else:
            raise Exception(f"{media_type} not supported")

    async def search(
        self,
        media_type: str,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        # TODO: implement pagination
        assert media_type in ("track", "playlist"), f"Cannot search for {media_type}"
        params = {
            "q": query,
            "facet": "genre",
            "user_id": USER_ID,
            "limit": limit,
            "offset": offset,
            "linked_partitioning": "1",
        }
        resp, status = await self._api_request(f"search/{media_type}s", params=params)
        assert status == 200
        if media_type == "track":
            for item in resp["collection"]:
                item["id"] = self._get_custom_id(item)
        return [resp]

    async def get_downloadable(self, item_info: str, _) -> SoundcloudDownloadable:
        # We have `get_metadata` overwrite the "id" field so that it contains
        # some extra information we need to download soundcloud tracks

        # item_id is the soundcloud ID of the track
        # download_url is either the url that points to an mp3 download or ""
        # if download_url == '_non_streamable' then we raise an exception

        infos: list[str] = item_info.split("|")
        logger.debug(f"{infos=}")
        assert len(infos) == 2, infos
        item_id, download_info = infos
        assert re.match(r"\d+", item_id) is not None

        if download_info == self.NON_STREAMABLE:
            raise NonStreamableError(item_info)

        if download_info == self.ORIGINAL_DOWNLOAD:
            resp_json, status = await self._api_request(f"tracks/{item_id}/download")
            assert status == 200
            return SoundcloudDownloadable(
                self.session,
                {"url": resp_json["redirectUri"], "type": "original"},
            )

        if download_info == self.NOT_RESOLVED:
            raise NotImplementedError(item_info)

        # download_info contains a transcoding url, which resolves to the
        # actual (signed) media url
        resp_json, status = await self._request(download_info)
        if status != 200 or "url" not in resp_json:
            raise NonStreamableError(f"Could not resolve stream for track {item_id}")
        file_type = "progressive" if "/progressive" in download_info else "mp3"
        return SoundcloudDownloadable(
            self.session,
            {"url": resp_json["url"], "type": file_type},
        )

    async def resolve_url(self, url: str) -> dict:
        """Get metadata of the item pointed to by a soundcloud url.

        This is necessary only for soundcloud because they don't store
        the item IDs in their url. See SoundcloudURL.into_pending for example
        usage.

        Args:
            url (str): Url to resolve.

        Returns:
            API response for item.
        """
        resp, status = await self._api_request("resolve", params={"url": url})
        assert status == 200
        if resp["kind"] == "track":
            resp["id"] = self._get_custom_id(resp)

        return resp

    async def _get_track(self, item_id: str):
        resp, status = await self._api_request(f"tracks/{item_id}")
        assert status == 200
        return resp

    async def _get_playlist(self, item_id: str):
        """A playlist, with every track's id replaced by its custom id."""
        resp, status = await self._api_request(f"playlists/{item_id}")
        assert status == 200

        # Only the first few tracks come with their metadata ("media"); fetch
        # the rest, MAX_BATCH_SIZE at a time.
        unresolved = [track["id"] for track in resp["tracks"] if "media" not in track]
        responses = await asyncio.gather(
            *(
                self._api_request("tracks", params={"ids": ",".join(map(str, batch))})
                for batch in itertools.batched(unresolved, MAX_BATCH_SIZE)
            )
        )
        assert all(status == 200 for _, status in responses)
        fetched = {track["id"]: track for tracks, _ in responses for track in tracks}

        for i, track in enumerate(resp["tracks"]):
            if "media" not in track:
                if track["id"] not in fetched:
                    raise Exception(f"Requested {track['id']} but got no response")
                # A copy: a track listed twice must not get its id rewritten twice.
                track = resp["tracks"][i] = dict(fetched[track["id"]])
            # Also when nothing needed fetching: a small playlist comes
            # complete, and a plain id can't be downloaded.
            track["id"] = self._get_custom_id(track)

        return resp

    @classmethod
    def _get_custom_id(cls, resp: dict) -> str:
        """The track's id and how to download it, as "id|how"."""
        item_id = resp["id"]
        assert "media" in resp, f"track {resp} should be resolved"

        if not resp["streamable"] or resp["policy"] == "BLOCK":
            return f"{item_id}|{cls.NON_STREAMABLE}"

        if resp["downloadable"] and resp["has_downloads_left"]:
            return f"{item_id}|{cls.ORIGINAL_DOWNLOAD}"

        # Prefer the plain progressive MP3 (one file), then HLS MP3 segments.
        # SoundCloud is phasing out MP3 HLS for many tracks, which used to trip
        # an assertion here.
        mp3 = [
            tc
            for tc in resp["media"].get("transcodings") or []
            if (tc.get("format") or {}).get("mime_type") == "audio/mpeg"
            and not tc.get("snipped")
        ]
        for protocol in ("progressive", "hls"):
            for tc in mp3:
                if tc["format"].get("protocol") == protocol:
                    return f"{item_id}|{tc['url']}"
        return f"{item_id}|{cls.NON_STREAMABLE}"

    def _auth_params(self) -> dict:
        c = self.config
        params = {"client_id": c.client_id, "app_locale": "en"}
        if c.app_version:
            params["app_version"] = c.app_version
        return params

    async def _api_request(self, path, params=None, headers=None):
        url = f"{BASE}/{path}"
        return await self._request(url, params=params, headers=headers)

    async def _request(self, url, params=None, headers=None) -> tuple[dict, int]:
        _params = self._auth_params()
        if params is not None:
            _params.update(params)

        logger.debug(f"Requesting {url} with {_params=}, {headers=}")

        async def read(resp):
            return await resp.json(), resp.status

        return await self._get_with_retries(url, read, _params, headers)

    async def _announce_success(self) -> bool:
        """Whether SoundCloud accepts the saved client id."""

        async def read(resp):
            return resp.status

        url = f"{BASE}/announcements"
        return await self._get_with_retries(url, read, self._auth_params()) == 200

    async def _refresh_tokens(self) -> tuple[str, str]:
        """Return a valid client_id, app_version pair.

        The client id is embedded in one of the JavaScript bundles loaded by
        soundcloud.com. Rather than relying on the exact markup of the script
        tags (which changed and broke this, upstream #1038), try every bundle,
        newest last, the way yt-dlp does.
        """
        page_text = await self._get_text_with_retries(STOCK_URL)

        app_version_match = re.search(r'__sc_version\s*=\s*"(\d+)"', page_text)
        app_version = app_version_match.group(1) if app_version_match else ""

        script_urls = re.findall(r'<script[^>]+src="([^"]+)"', page_text)
        for script_url in reversed(script_urls):
            if script_url.startswith("//"):
                script_url = "https:" + script_url
            elif script_url.startswith("/"):
                script_url = STOCK_URL.rstrip("/") + script_url
            try:
                script = await self._get_text_with_retries(script_url)
            except Exception as e:
                logger.debug("Could not fetch %s: %s", script_url, e)
                continue
            client_id = _find_client_id(script)
            if client_id is not None:
                logger.debug(
                    f"Refreshed soundcloud tokens as {client_id=} {app_version=}"
                )
                return client_id, app_version

        raise Exception(
            f"Could not find a SoundCloud client id in {len(script_urls)} scripts "
            f"on {STOCK_URL}. SoundCloud may have changed its website."
        )


CLIENT_ID_REGEXES = (
    re.compile(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"'),
    re.compile(r"client_id=([0-9a-zA-Z]{32})"),
)


def _find_client_id(script: str) -> str | None:
    for regex in CLIENT_ID_REGEXES:
        match = regex.search(script)
        if match is not None:
            return match.group(1)
    return None
