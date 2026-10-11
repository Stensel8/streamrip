import logging
import os
from dataclasses import dataclass

from .. import progress
from ..client import BasicDownloadable, Client
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filename
from ..metadata import AlbumMetadata
from ..metadata.util import format_quality, get_album_track_ids
from .artwork import download_artwork
from .media import Media, Pending, rip_tracks
from .track import PendingTrack, album_folder

logger = logging.getLogger("streamrip")

# Tracks of an album resolved at once; more only delays the first download,
# and also means more requests landing on the rate limit in the same instant.
RESOLVE_CONCURRENCY = 3


async def download_booklets(session, booklets: list[dict], folder: str):
    """Save an album's PDF booklets (Qobuz "goodies") next to its tracks."""
    pdfs = [
        (url, clean_filename(b.get("description") or b.get("name") or "Booklet"))
        for b in booklets
        if (url := b.get("url") or b.get("original_url") or "").lower().endswith(".pdf")
    ]
    names = [name for _, name in pdfs]
    for n, (url, name) in enumerate(pdfs, 1):
        if names.count(name) > 1:
            name = f"{name} {n}"
        path = os.path.join(folder, f"{name}.pdf")
        if os.path.isfile(path):
            continue
        try:
            await BasicDownloadable(session, url, "pdf").download(path, lambda _: None)
        except Exception as e:
            # A missing booklet is never worth the album.
            logger.warning(f"Could not download booklet {url}: {type(e).__name__}: {e}")


@dataclass(slots=True)
class Album(Media):
    meta: AlbumMetadata
    tracks: list[PendingTrack]
    config: Config
    # folder where the tracks will be downloaded
    folder: str
    db: Database
    client: Client | None = None
    skipped_tracks: int = 0

    def _title(self) -> str:
        """Return the album's title, suffixed with its quality label."""
        quality = format_quality(
            self.meta.info.container,
            self.meta.info.bit_depth,
            self.meta.info.sampling_rate,
        )
        return f"{self.meta.album} {quality}"

    async def preprocess(self):
        """Count skips for this selected album, register progress, and fetch booklets."""
        self.db.skipped_now += self.skipped_tracks
        total = self.skipped_tracks + len(self.tracks)
        if self.skipped_tracks and not self.tracks:
            logger.info(
                f"{self.meta.album}: all {total} tracks already downloaded, skipping"
            )
        elif self.skipped_tracks:
            logger.info(
                f"{self.meta.album}: skipping {self.skipped_tracks} of {total} tracks "
                "already downloaded"
            )
        progress.add_title(
            id(self), self._title(), self.config.session.cli.progress_bars
        )
        # Here, not when the album is resolved: artists and labels resolve every
        # album before their filters drop some, and those get no booklets. Only
        # Qobuz albums have any; a finished album has no folder to put them in.
        if (
            self.tracks
            and self.client is not None
            and self.meta.info.booklets
            and self.config.session.qobuz.download_booklets
        ):
            await download_booklets(
                self.client.session, self.meta.info.booklets, self.folder
            )

    async def download(self):
        """Resolve and download every track of the album."""
        await rip_tracks(
            self.tracks,
            RESOLVE_CONCURRENCY,
            self.config.session.metadata.prefer_explicit,
            self.config.session.cli.progress_bars,
        )
        # One line for every album, however small, so the scrollback is a
        # complete record of the run -- failed tracks log their own errors.
        # (An album with nothing left to download already logged a skip.)
        if self.tracks:
            console.log(f"Finished {self.meta.albumartist} - {self._title()}")

    async def postprocess(self):
        progress.remove_title(id(self), self.config.session.cli.progress_bars)


@dataclass(slots=True)
class PendingAlbum(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Album | None:
        try:
            resp = await self.client.get_metadata(self.id, "album")
        except NonStreamableError as e:
            logger.error(
                f"Album {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = AlbumMetadata.from_album_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for {self.id}: {e}")
            return None

        if meta is None:
            logger.error(
                f"Album {self.id} not available to stream on {self.client.source}",
            )
            return None

        tracklist = get_album_track_ids(self.client.source, resp)
        # Tracks already in the database would each be skipped, and logged, one
        # by one. Say so once for the album instead, and don't fetch a cover or
        # make a folder for an album with nothing left to download.
        source = self.client.source
        todo = [t for t in tracklist if not self.db.downloaded(source, t)]
        done = len(tracklist) - len(todo)
        folder = album_folder(self.config, self.client.source, meta)
        if tracklist and not todo:
            return Album(meta, [], self.config, folder, self.db, skipped_tracks=done)
        os.makedirs(folder, exist_ok=True)
        embed_cover, _ = await download_artwork(
            self.client.session,
            folder,
            meta.covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        pending_tracks = [
            PendingTrack(
                track_id,
                album=meta,
                client=self.client,
                config=self.config,
                folder=folder,
                db=self.db,
                cover_path=embed_cover,
            )
            for track_id in todo
        ]
        logger.debug("Pending tracks: %s", pending_tracks)
        return Album(
            meta,
            pending_tracks,
            self.config,
            folder,
            self.db,
            self.client,
            skipped_tracks=done,
        )
