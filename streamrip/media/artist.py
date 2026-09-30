import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from ..client import Client
from ..config import ArtistFilterConfig, Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..metadata import ArtistMetadata
from .album import Album, PendingAlbum
from .media import Media, Pending

logger = logging.getLogger("streamrip")

# One album at a time: downloading several concurrently interleaves their
# tracks in the progress display with no way to tell which track is from
# which album, and it invites the streaming service's rate limit besides.
RESOLVE_CHUNK_SIZE = 1


async def rip_albums(
    albums: list[PendingAlbum], wanted: Callable[[Album], bool] = lambda _: True
):
    """Resolve and download albums a few at a time; a failure costs one album."""
    # Sliding window, not batches: the next album starts as soon as one finishes.
    window = asyncio.Semaphore(RESOLVE_CHUNK_SIZE)

    async def _rip(item: PendingAlbum):
        """Resolve and download one album, dropping it if unwanted."""
        async with window:
            try:
                album = await item.resolve()
                if album is not None and wanted(album):
                    await album.rip()
            except Exception as e:
                logger.error(f"Error downloading album: {type(e).__name__}: {e}")

    await asyncio.gather(*map(_rip, albums))


@dataclass(slots=True)
class Artist(Media):
    """An artist's discography: a list of albums, optionally filtered."""

    name: str
    albums: list[PendingAlbum]
    client: Client
    config: Config

    async def preprocess(self):
        pass

    async def download(self):
        """Resolve and download every wanted album of the discography."""
        # Fetching each album's tracklist happens RESOLVE_CHUNK_SIZE at a
        # time before the first progress bar appears, which for an artist
        # with a large discography can take a while with nothing on screen
        # to show for it -- so say up front what's queued.
        console.print(
            f"[bold]{self.name}[/bold]: found {len(self.albums)} release(s), "
            "resolving and downloading..."
        )
        filter_conf = self.config.session.artist_filters
        if filter_conf.repeats:
            console.log(
                "Resolving [purple]ALL[/purple] artist albums to detect repeats. This may take a while."
            )
            await self._resolve_then_download(filter_conf)
        else:
            await self._download_async(filter_conf)

    async def postprocess(self):
        pass

    async def _resolve_then_download(self, filters: ArtistFilterConfig):
        """Resolve all artist albums, then download.

        Used when the repeats filter is on, which needs every album's title
        before it can pick one per group. Resolves still go through the same
        window as downloads, so this doesn't burst past the rate limit either.
        """
        window = asyncio.Semaphore(RESOLVE_CHUNK_SIZE)

        async def _resolve(item: PendingAlbum) -> Album | None:
            """Resolve one album, returning None on failure."""
            async with window:
                try:
                    return await item.resolve()
                except Exception as e:
                    logger.error(f"Error resolving album: {type(e).__name__}: {e}")
                    return None

        resolved = await asyncio.gather(*map(_resolve, self.albums))
        albums = [a for a in resolved if a is not None]
        if filters.repeats:
            albums = self._filter_repeats(albums)

        async def _rip(album: Album):
            """Download one already-resolved album."""
            async with window:
                try:
                    await album.rip()
                except Exception as e:
                    logger.error(f"Error downloading album: {type(e).__name__}: {e}")

        await asyncio.gather(*[_rip(a) for a in albums if self._wanted(a, filters)])

    async def _download_async(self, filters: ArtistFilterConfig):
        """Resolve and download albums one at a time, without repeats filtering."""
        await rip_albums(self.albums, lambda a: self._wanted(a, filters))

    def _wanted(self, a: Album, f: ArtistFilterConfig) -> bool:
        """Whether an album passes every enabled filter except repeats."""
        return not (
            (f.extras and not self._extras(a))
            or (f.features and not self._features(a))
            or (f.non_remaster and not self._non_remaster(a))
            or (f.non_albums and not self._non_albums(a))
        )

    @staticmethod
    def _filter_repeats(albums: list[Album]) -> list[Album]:
        """When there are different versions of an album on the artist,
        choose the one with the best quality.

        Two albums are versions of each other if their titles match up to the
        first bracket or parenthesis ("X" and "X (Deluxe)").
        """
        groups: dict[str, list[Album]] = {}
        for a in albums:
            # A title that starts with a bracket keeps it, or every such
            # title would land in one group.
            title = re.split(r"[(\[]", a.meta.album, maxsplit=1)[0].strip()
            groups.setdefault((title or a.meta.album).lower(), []).append(a)

        return [
            max(
                group,
                key=lambda a: (
                    a.meta.info.bit_depth or 0,
                    a.meta.info.sampling_rate or 0,
                    a.meta.info.explicit,
                ),
            )
            for group in groups.values()
        ]

    _extra_re = re.compile(
        r"(?i)(anniversary|deluxe|live|collector|demo|expanded|remix)"
    )

    # ----- Filter predicates -----
    def _features(self, a: Album) -> bool:
        """Filter out features."""
        return a.meta.albumartist == self.name

    def _extras(self, a: Album) -> bool:
        """Filter out extras: special editions, live albums, remixes and the
        like (see `_extra_re`), and various-artists compilations.
        """
        return (
            a.meta.albumartist != "Various Artists"
            and self._extra_re.search(a.meta.album) is None
        )

    _remaster_re = re.compile(r"(?i)(re)?master(ed)?")

    def _non_remaster(self, a: Album) -> bool:
        """Filter out albums that are not remasters."""
        return self._remaster_re.search(a.meta.album) is not None

    def _non_albums(self, a: Album) -> bool:
        """Filter out singles."""
        # Not len(a.tracks): tracks already downloaded aren't in that list.
        return a.meta.tracktotal > 1


@dataclass(slots=True)
class PendingArtist(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Artist | None:
        try:
            resp = await self.client.get_metadata(self.id, "artist")
        except NonStreamableError as e:
            logger.error(
                f"Artist {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = ArtistMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error(
                f"Error building artist metadata: {e}",
            )
            return None

        albums = [
            PendingAlbum(album_id, self.client, self.config, self.db)
            for album_id in meta.album_ids()
        ]
        return Artist(meta.name, albums, self.client, self.config)
