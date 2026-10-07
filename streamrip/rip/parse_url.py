from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from ..client import Client, SoundcloudClient, new_session
from ..config import Config
from ..db import Database
from ..media import Pending, PendingArtist, PendingPlaylist, pending_item

logger = logging.getLogger("streamrip")
URL_REGEX = re.compile(
    r"https?://(?:www|open|play|listen)?\.?(qobuz|tidal|deezer)\.com?(?:(?:/(album|artist|track|playlist|video|label))|(?:\/[-\w]+?))+\/([-\w]+)",
)
TIDAL_SHARE_SUFFIX_REGEX = re.compile(r"^(https?://[^/]*tidal\.com/.+?)/u/?$")
SOUNDCLOUD_URL_REGEX = re.compile(r"https://soundcloud.com/[-\w:/]+")
# open.spotify.com links (with an optional "intl-xx/" and the old "user/name/" in
# them) and spotify:track:... URIs. A Spotify id is 22 letters and digits.
SPOTIFY_URL_REGEX = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(?:user/[^/]+/)?"
    r"|spotify:(?:user:[^:]+:)?)(track|album|artist|playlist)[/:]([0-9A-Za-z]{22})(?![0-9A-Za-z])",
)
QOBUZ_INTERPRETER_URL_REGEX = re.compile(
    r"https?://www\.qobuz\.com/\w\w-\w\w/interpreter/[-\w]+/([-\w]+)",
)


class URL(ABC):
    match: re.Match
    source: str

    def __init__(self, match: re.Match, source: str):
        self.match = match
        self.source = source

    @classmethod
    @abstractmethod
    def from_str(cls, url: str) -> URL | None:
        raise NotImplementedError

    @abstractmethod
    async def into_pending(
        self,
        client: Client,
        config: Config,
        db: Database,
    ) -> Pending:
        raise NotImplementedError


class GenericURL(URL):
    @classmethod
    def from_str(cls, url: str) -> URL | None:
        # Tidal's share sheet produces links ending in "/u". URL_REGEX takes
        # the last path segment as the item id -- it has to, because Qobuz
        # album urls look like /<locale>/album/<slug>/<id> -- so that suffix
        # would be parsed as an id of "u" and the API would 404.
        url = TIDAL_SHARE_SUFFIX_REGEX.sub(r"\1", url)

        generic_url = URL_REGEX.match(url)
        if generic_url is None:
            return None

        source, media_type, item_id = generic_url.groups()
        if source is None or media_type is None or item_id is None:
            return None

        return cls(generic_url, source)

    async def into_pending(
        self,
        client: Client,
        config: Config,
        db: Database,
    ) -> Pending:
        """Make the pending item this URL names.

        Its source, media type and id come from the match.
        """
        source, media_type, item_id = self.match.groups()
        assert client.source == source
        return pending_item(media_type, item_id, client, config, db)


class QobuzInterpreterURL(URL):
    interpreter_artist_regex = re.compile(r"getSimilarArtist\(\s*'(\w+)'")

    @classmethod
    def from_str(cls, url: str) -> URL | None:
        qobuz_interpreter_url = QOBUZ_INTERPRETER_URL_REGEX.match(url)
        if qobuz_interpreter_url is None:
            return None

        return cls(qobuz_interpreter_url, "qobuz")

    async def into_pending(
        self,
        client: Client,
        config: Config,
        db: Database,
    ) -> Pending:
        """Resolve the interpreter URL's artist id, fetching the page if needed."""
        url = self.match.group(0)
        possible_id = self.match.group(1)
        if possible_id.isdigit():
            logger.debug("Found artist ID %s in interpreter url %s", possible_id, url)
            artist_id = possible_id
        else:
            artist_id = await self.extract_interpreter_url(
                url, verify_ssl=config.session.downloads.verify_ssl
            )
        return PendingArtist(artist_id, client, config, db)

    @staticmethod
    async def extract_interpreter_url(url: str, verify_ssl: bool = True) -> str:
        """The artist id on a Qobuz interpreter page, such as
        https://www.qobuz.com/us-en/interpreter/{artist}/download-streaming-albums
        """
        # Public pages must never inherit the API session's account credentials.
        url = url.replace("http://", "https://", 1)
        async with new_session(verify_ssl=verify_ssl) as session:
            # Do not let a redirect downgrade the request to plaintext HTTP.
            async with session.get(url, allow_redirects=False) as resp:
                if 300 <= resp.status < 400:
                    raise ValueError(
                        "Qobuz interpreter URL redirected. Use a URL that contains "
                        "an artist id."
                    )
                resp.raise_for_status()
                match = QobuzInterpreterURL.interpreter_artist_regex.search(
                    await resp.text(),
                )

        if match:
            return match.group(1)

        raise Exception(
            "Unable to extract artist id from interpreter url. Use a "
            "url that contains an artist id.",
        )


class DeezerDynamicURL(URL):
    standard_link_re = re.compile(
        r"https://www\.deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(album|artist|playlist|track)/(\d+)"
    )
    # Share links: the old Firebase ones (deezer.page.link / dzr.page.link)
    # and the current link.deezer.com/s/... ones (upstream #865, #818).
    dynamic_link_re = re.compile(
        r"https://(?:(?:deezer|dzr)\.page\.link|link\.deezer\.com/s)/[\w-]+"
    )

    @classmethod
    def from_str(cls, url: str) -> URL | None:
        match = cls.dynamic_link_re.match(url)
        if match is None:
            return None

        return cls(match, "deezer")

    async def into_pending(
        self,
        client: Client,
        config: Config,
        db: Database,
    ) -> Pending:
        """Follow the dynamic link to the item it points at and make that pending."""
        url = self.match.group(0)  # entire dynamic link
        media_type, item_id = await self._extract_info_from_dynamic_link(url, client)
        return pending_item(media_type, item_id, client, config, db)

    @classmethod
    async def _extract_info_from_dynamic_link(
        cls, url: str, client: Client
    ) -> tuple[str, str]:
        """The (media type, item id) a Deezer share link points to."""
        async with client.session.get(url) as resp:
            # Share links redirect to the regular www.deezer.com URL, which is
            # the most reliable place to read the id from; fall back to the
            # page body for the older Firebase links.
            match = cls.standard_link_re.search(str(resp.url))
            if match is None:
                match = cls.standard_link_re.search(await resp.text())

        if match:
            return match.group(1), match.group(2)

        raise Exception(f"Unable to extract the Deezer item from {url}.")


class DeezerFavoriteURL(URL):
    """Matches Deezer liked-tracks profile URLs.

    Example: https://www.deezer.com/fr/profile/1234567/loved
    """

    favorite_re = re.compile(
        r"https://(?:www\.)?deezer\.com/[a-z]{2}/profile/(\d+)/loved"
    )

    @classmethod
    def from_str(cls, url: str) -> URL | None:
        match = cls.favorite_re.match(url)
        if match is None:
            return None
        return cls(match, "deezer")

    async def into_pending(
        self,
        client: Client,
        config: Config,
        db: Database,
    ) -> Pending:
        user_id = self.match.group(1)
        return PendingPlaylist(f"favorites:{user_id}", client, config, db)


class SoundcloudURL(URL):
    source = "soundcloud"

    def __init__(self, url: str):
        self.url = url

    async def into_pending(
        self,
        client: SoundcloudClient,
        config: Config,
        db: Database,
    ) -> Pending:
        """Resolve the URL on SoundCloud: a track or a playlist, nothing else."""
        resolved = await client.resolve_url(self.url)
        if resolved["kind"] not in ("track", "playlist"):
            raise NotImplementedError(resolved["kind"])
        return pending_item(resolved["kind"], str(resolved["id"]), client, config, db)

    @classmethod
    def from_str(cls, url: str):
        """The SoundCloud URL in `url`, or None if it isn't one."""
        soundcloud_url = SOUNDCLOUD_URL_REGEX.match(url)
        if soundcloud_url is None:
            return None
        return cls(soundcloud_url.group(0))


class SpotifyURL(URL):
    """An open.spotify.com link or a spotify: URI of a track, album, artist or playlist."""

    @classmethod
    def from_str(cls, url: str) -> URL | None:
        """The Spotify item `url` names, or None if it isn't one."""
        match = SPOTIFY_URL_REGEX.match(url)
        if match is None:
            return None
        return cls(match, "spotify")

    async def into_pending(
        self,
        client: Client,
        config: Config,
        db: Database,
    ) -> Pending:
        """Make the pending item this URL names, by the id in it."""
        media_type, item_id = self.match.groups()
        return pending_item(media_type, item_id, client, config, db)


def parse_url(url: str) -> URL | None:
    """The URL type that matches url, or None if none does."""
    url = url.strip()
    parsed_urls: list[URL | None] = [
        GenericURL.from_str(url),
        QobuzInterpreterURL.from_str(url),
        SoundcloudURL.from_str(url),
        SpotifyURL.from_str(url),
        DeezerDynamicURL.from_str(url),
        DeezerFavoriteURL.from_str(url),
    ]
    return next((u for u in parsed_urls if u is not None), None)
