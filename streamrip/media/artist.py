"""The albums of an artist or a record label, downloaded one at a time."""

import logging
import re
from dataclasses import dataclass
lazy from collections.abc import Callable

from ..console import console
from ..metadata import ArtistMetadata, LabelMetadata
from .album import Album, PendingAlbum
from .media import Media, PendingItem
lazy from ..client import Client
lazy from ..config import ArtistFilterConfig, Config

logger = logging.getLogger("streamrip")


def announce(name: str, albums: list) -> None:
    """Say what's queued: resolving the albums can take a while before the
    first progress bar appears.
    """
    console.print(
        f"[bold]{name}[/bold]: found {len(albums)} release(s), "
        "resolving and downloading..."
    )


async def rip_albums(
    albums: list[PendingAlbum],
    wanted: Callable[[Album], bool] = lambda _: True,
):
    """Resolve and download albums one at a time; a failure costs one album.

    One at a time, like the items of a run (rip/main.py): an album's tracks
    never mix with another's on screen, or on the rate limit.
    """
    # No album-count bar here: each album adds its tracks to the one overall
    # bar as it downloads (media.rip_tracks), so the discography shows the same
    # unified track progress as a single album or playlist.
    for item in albums:
        try:
            album = await item.resolve()
            if album is not None and wanted(album):
                await album.rip()
        except Exception as e:
            logger.error("Error downloading album: %s: %s", type(e).__name__, e)


@dataclass(slots=True)
class Artist(Media):
    """An artist's discography: a list of albums, optionally filtered."""

    name: str
    albums: list[PendingAlbum]
    client: Client
    config: Config

    async def download(self):
        """Resolve and download every wanted album of the discography."""
        announce(self.name, self.albums)
        filter_conf = self.config.session.artist_filters
        if filter_conf.repeats:
            console.log(
                "Resolving [purple]ALL[/purple] artist albums to detect repeats. This may take a while."
            )
            await self._resolve_then_download(filter_conf)
        else:
            await self._download_async(filter_conf)

    async def _resolve_then_download(self, filters: ArtistFilterConfig):
        """Resolve all artist albums, then download.

        Used when the repeats filter is on, which needs every album's title
        before it can pick one per group. Albums still resolve one at a time,
        so this doesn't burst past the rate limit either.
        """
        albums = []
        for item in self.albums:
            try:
                if (album := await item.resolve()) is not None:
                    albums.append(album)
            except Exception as e:
                logger.error("Error resolving album: %s: %s", type(e).__name__, e)
        if filters.repeats:
            albums = self._filter_repeats(albums)
        albums = [a for a in albums if self._wanted(a, filters)]

        for album in albums:
            try:
                await album.rip()
            except Exception as e:
                logger.error("Error downloading album: %s: %s", type(e).__name__, e)

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
class Label(Media):
    """A record label's catalog: a list of albums."""

    name: str
    albums: list[PendingAlbum]
    client: Client
    config: Config

    async def download(self):
        """Resolve and download every album in the label's catalog."""
        announce(self.name, self.albums)
        await rip_albums(self.albums)


def _pending_albums(item: PendingItem, ids: list[str]) -> list[PendingAlbum]:
    """The albums of an artist or label, each to be resolved in its turn."""
    return [
        PendingAlbum(album_id, item.client, item.config, item.db) for album_id in ids
    ]


@dataclass(slots=True)
class PendingArtist(PendingItem):
    async def resolve(self) -> Artist | None:
        """Fetch the artist and its releases; None if that fails."""
        if (resp := await self._fetch("artist")) is None:
            return None
        try:
            meta = ArtistMetadata.from_resp(
                resp,
                self.client.source,
                self.config.session.metadata.prefer_explicit,
            )
        except Exception as e:
            logger.error("Error building artist metadata: %s", e)
            return None
        return Artist(
            meta.name, _pending_albums(self, meta.ids), self.client, self.config
        )


@dataclass(slots=True)
class PendingLabel(PendingItem):
    async def resolve(self) -> Label | None:
        """Fetch the label and its whole catalog; None if that fails."""
        if (resp := await self._fetch("label")) is None:
            return None
        try:
            meta = LabelMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error("Error building label metadata: %s", e)
            return None
        return Label(
            meta.name, _pending_albums(self, meta.ids), self.client, self.config
        )
