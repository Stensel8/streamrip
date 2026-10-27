import asyncio
import itertools
import logging
import random
import re
from typing import Any
from urllib.parse import urljoin

lazy import m3u8

from ..exceptions import APIError, NonStreamableError
from .client import Client
from .downloadable import SegmentedMP4Downloadable, SoundcloudDownloadable

# e.g. 123456-293847-121314-209849
USER_ID = "-".join(str(random.randint(111111, 999999)) for _ in range(4))
BASE = "https://api-v2.soundcloud.com"
STOCK_URL = "https://soundcloud.com/"

# for playlists
MAX_BATCH_SIZE = 50

# The streams streamrip can download whole, by MIME type, AAC first: it is all
# many tracks still have, and at 160 kbps the best SoundCloud gives without a
# login (its MP3 is 128). The file each makes.
STREAM_EXTENSIONS = frozendict({"audio/mp4": "m4a", "audio/mpeg": "mp3"})
QUALITY_RANK = frozendict({"hq": 0, "sq": 1, "lq": 2})

logger = logging.getLogger("streamrip")


class SoundcloudClient(Client):
    """SoundCloud: anonymous, with the client id of its own web player."""

    source = "soundcloud"

    NON_STREAMABLE = "_non_streamable"
    ORIGINAL_DOWNLOAD = "_original_download"

    async def login(self):
        """Use the saved client id if it still works, else scrape a new one."""
        self._ensure_session()
        client_id, app_version = self.config.client_id, self.config.app_version
        # app_version is optional: SoundCloud does not always expose it, and
        # requiring it made every run re-scrape the website.
        if not client_id or not (await self._announce_success()):
            client_id, app_version = await self._refresh_tokens()
            # In this session, and in the config file written on exit.
            for c in (self.config, self.global_config.file.soundcloud):
                c.client_id, c.app_version = client_id, app_version
            self.global_config.file.set_modified()

        logger.debug(
            "Current valid client_id=%s app_version=%s", client_id, app_version
        )
        self.logged_in = True

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        """A track (by its custom id), or a playlist with its tracks' ids made
        custom ids."""
        if media_type == "track":
            track_id, _ = item_id.split("|")
            resp = await self._get_track(track_id)
            # What the download makes, for the folder name and the progress
            # bar (AlbumMetadata.from_soundcloud reads it).
            stream = self._stream(resp) or ""
            resp["container"] = "AAC" if stream.startswith("m4a:") else "MP3"
            return resp
        if media_type == "playlist":
            return await self._get_playlist(item_id)
        raise ValueError(f"SoundCloud has no {media_type}s")

    async def search(
        self,
        media_type: str,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        if media_type not in {"track", "playlist"}:
            raise ValueError(f"Cannot search SoundCloud for {media_type}s")
        params = {
            "q": query,
            "facet": "genre",
            "user_id": USER_ID,
            "limit": limit,
            "offset": offset,
            "linked_partitioning": "1",
        }
        resp = await self._request_ok(f"search/{media_type}s", params)
        if media_type == "track":
            for item in resp["collection"]:
                item["id"] = self._get_custom_id(item)
        return [resp]

    async def get_downloadable(self, item_id: str, quality: int):
        # item_id is the custom id _get_custom_id made: "id|how", how being
        # NON_STREAMABLE, ORIGINAL_DOWNLOAD or a stream (see _stream).
        track_id, how = item_id.split("|")

        if how == self.NON_STREAMABLE:
            raise NonStreamableError(item_id)

        if how == self.ORIGINAL_DOWNLOAD:
            status, resp = await self._api_request(f"tracks/{track_id}/download")
            if status == 200:
                return SoundcloudDownloadable(
                    self.session, resp["redirectUri"], "original"
                )
            # Anonymously, this endpoint now answers 401 even for tracks marked
            # downloadable (the original needs a login), which made them fail
            # outright. Fall back to the stream they also have.
            track_status, track = await self._api_request(f"tracks/{track_id}")
            stream = self._stream(track) if track_status == 200 else None
            if stream is None:
                raise NonStreamableError(
                    f"SoundCloud original download unavailable for track {track_id} "
                    f"(HTTP {status}), and no stream to fall back to"
                )
            logger.info(
                "SoundCloud original of track %s unavailable (HTTP %s); "
                "downloading the stream instead",
                track_id,
                status,
            )
            how = stream

        # "<extension>:<transcoding url>"; the transcoding url resolves to the
        # actual (signed) media url
        extension, _, transcoding_url = how.partition(":")
        status, resp = await self._request(transcoding_url)
        if status != 200 or "url" not in resp:
            raise NonStreamableError(f"Could not resolve stream for track {track_id}")
        url = resp["url"]
        if "/progressive" in transcoding_url:
            return SoundcloudDownloadable(self.session, url, "progressive", extension)
        if extension == "mp3":
            return SoundcloudDownloadable(self.session, url, "hls")
        return await self._aac_stream(url)

    async def _aac_stream(self, url: str) -> SegmentedMP4Downloadable:
        """AAC over HLS: fragmented MP4, an init segment and media segments."""
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            segments = m3u8.loads(await resp.text(), uri=url).segments
        if not segments or segments[0].init_section is None:
            raise NonStreamableError(f"SoundCloud AAC stream without segments: {url}")
        return SegmentedMP4Downloadable(
            self.session,
            init_url=segments[0].init_section.absolute_uri,
            segment_urls=[s.absolute_uri for s in segments],
            codec="aac",
            source=self.source,
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
        resp = await self._request_ok("resolve", {"url": url})
        if resp["kind"] == "track":
            resp["id"] = self._get_custom_id(resp)

        return resp

    async def _get_track(self, item_id: str) -> dict:
        """A track's metadata from SoundCloud's API."""
        return await self._request_ok(f"tracks/{item_id}")

    async def _get_playlist(self, item_id: str) -> dict:
        """A playlist, with every track's id replaced by its custom id."""
        resp = await self._request_ok(f"playlists/{item_id}")

        # Only the first few tracks come with their metadata ("media"); fetch
        # the rest, MAX_BATCH_SIZE at a time.
        unresolved = [track["id"] for track in resp["tracks"] if "media" not in track]
        batches = await asyncio.gather(
            *(
                self._request_ok("tracks", {"ids": ",".join(map(str, batch))})
                for batch in itertools.batched(unresolved, MAX_BATCH_SIZE, strict=False)
            )
        )
        fetched = {track["id"]: track for tracks in batches for track in tracks}

        for i, listed in enumerate(resp["tracks"]):
            track = listed
            if "media" not in listed:
                if listed["id"] not in fetched:
                    raise APIError(f"SoundCloud returned no track {listed['id']}")
                # A copy: a track listed twice must not get its id rewritten twice.
                track = resp["tracks"][i] = dict(fetched[listed["id"]])
            # Also when nothing needed fetching: a small playlist comes
            # complete, and a plain id can't be downloaded.
            track["id"] = self._get_custom_id(track)

        return resp

    @classmethod
    def _get_custom_id(cls, resp: dict) -> str:
        """The track's id and how to download it, as "id|how"."""
        item_id = resp["id"]
        if not resp["streamable"] or resp["policy"] == "BLOCK":
            return f"{item_id}|{cls.NON_STREAMABLE}"

        if resp["downloadable"] and resp["has_downloads_left"]:
            return f"{item_id}|{cls.ORIGINAL_DOWNLOAD}"

        return f"{item_id}|{cls._stream(resp) or cls.NON_STREAMABLE}"

    @staticmethod
    def _stream(resp: dict) -> str | None:
        """A track's best full stream, as "<extension>:<transcoding url>".

        The highest quality, AAC before MP3 at the same quality, and one file
        before HLS segments. Previews, the adaptive "abr" stream (a playlist's
        MIME type) and DRM (ctr-/cbc-encrypted-hls) can't be downloaded whole.
        """
        streams = [
            tc
            for tc in (resp.get("media") or {}).get("transcodings") or []
            if _mime(tc) in STREAM_EXTENSIONS
            and tc["format"].get("protocol") in {"progressive", "hls"}
            and not tc.get("snipped")
        ]
        if not streams:
            return None
        best = min(
            streams,
            key=lambda tc: (
                QUALITY_RANK.get(tc.get("quality"), len(QUALITY_RANK)),
                list(STREAM_EXTENSIONS).index(_mime(tc)),
                tc["format"]["protocol"] != "progressive",
            ),
        )
        return f"{STREAM_EXTENSIONS[_mime(best)]}:{best['url']}"

    def _auth_params(self) -> dict:
        c = self.config
        params = {"client_id": c.client_id, "app_locale": "en"}
        if c.app_version:
            params["app_version"] = c.app_version
        return params

    async def _request_ok(self, path: str, params: dict | None = None) -> Any:
        """_api_request that insists on HTTP 200."""
        status, resp = await self._api_request(path, params)
        if status != 200:
            raise APIError(f"SoundCloud {path} failed (HTTP {status})")
        return resp

    async def _api_request(
        self, path: str, params: dict | None = None
    ) -> tuple[int, Any]:
        """GET a SoundCloud API path: (status, JSON body), whatever the status."""
        return await self._request(f"{BASE}/{path}", params)

    async def _request(self, url: str, params: dict | None = None) -> tuple[int, Any]:
        """GET a SoundCloud url with the client id: (status, JSON body)."""
        logger.debug("Requesting %s with params=%s", url, params)
        return await self._get_json(url, self._auth_params() | (params or {}))

    async def _announce_success(self) -> bool:
        """Whether SoundCloud accepts the saved client id."""

        async def read(resp):
            """The response's HTTP status."""
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
        # "//host/x.js", "/x.js" or a whole url: each an absolute url after this.
        for script_url in (urljoin(STOCK_URL, src) for src in reversed(script_urls)):
            try:
                script = await self._get_text_with_retries(script_url)
            except Exception as e:
                logger.debug("Could not fetch %s: %s", script_url, e)
                continue
            client_id = _find_client_id(script)
            if client_id is not None:
                logger.debug(
                    "Refreshed soundcloud tokens as %s %s", client_id, app_version
                )
                return client_id, app_version

        raise APIError(
            f"Could not find a SoundCloud client id in {len(script_urls)} scripts "
            f"on {STOCK_URL}. SoundCloud may have changed its website."
        )


CLIENT_ID_REGEXES = (
    re.compile(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"'),
    re.compile(r"client_id=([0-9a-zA-Z]{32})"),
)


def _mime(transcoding: dict) -> str:
    """A transcoding's MIME type without its codecs, like "audio/mp4"."""
    mime = (transcoding.get("format") or {}).get("mime_type") or ""
    return mime.partition(";")[0].strip()


def _find_client_id(script: str) -> str | None:
    """The client id in a SoundCloud script, or None."""
    for regex in CLIENT_ID_REGEXES:
        match = regex.search(script)
        if match is not None:
            return match.group(1)
    return None
