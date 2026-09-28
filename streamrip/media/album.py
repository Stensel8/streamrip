import asyncio
import logging
import os
from dataclasses import dataclass

from .. import progress
from ..client import Client
from ..config import Config
from ..db import Database
from ..exceptions import NonStreamableError, TrackDownloadFailedError
from ..filepath_utils import clean_filepath
from ..metadata import AlbumMetadata
from ..metadata.util import get_album_track_ids
from .artwork import download_artwork
from .media import Media, Pending, filter_prefer_explicit, resolve_or_none
from .track import PendingTrack

logger = logging.getLogger("streamrip")

# Tracks of an album resolved at once; more only delays the first download.
RESOLVE_CONCURRENCY = 4


@dataclass(slots=True)
class Album(Media):
    meta: AlbumMetadata
    tracks: list[PendingTrack]
    config: Config
    # folder where the tracks will be downloaded
    folder: str
    db: Database

    async def preprocess(self):
        progress.add_title(self.meta.album)

    async def download(self):
        if self.config.session.metadata.prefer_explicit:
            await self._resolve_then_download()
            return

        resolve_slots = asyncio.Semaphore(RESOLVE_CONCURRENCY)

        async def _resolve_and_download(pending: Pending):
            try:
                async with resolve_slots:
                    track = await pending.resolve()
                if track is None:
                    return
                await track.rip()
            except TrackDownloadFailedError:
                pass  # already logged and recorded by Track.download()
            except Exception as e:
                # Include the type: some exceptions have an empty message,
                # which used to log as "Error downloading track: ''" (#938).
                logger.error(f"Error downloading track: {type(e).__name__}: {e}")

        results = await asyncio.gather(
            *[_resolve_and_download(p) for p in self.tracks], return_exceptions=True
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Album track processing error: {result}")

    async def _resolve_then_download(self):
        """Resolve every track before downloading any of them, so a clean
        copy can be dropped in favor of an explicit one once both are known
        (see [metadata] prefer_explicit). Costs one extra API call per track
        compared to the default resolve-and-download-immediately path.
        """
        resolve_slots = asyncio.Semaphore(RESOLVE_CONCURRENCY)

        async def _resolve(pending: Pending):
            async with resolve_slots:
                return await resolve_or_none(pending)

        resolved = await asyncio.gather(*[_resolve(p) for p in self.tracks])
        tracks = filter_prefer_explicit([t for t in resolved if t is not None])

        async def _download(track):
            try:
                await track.rip()
            except TrackDownloadFailedError:
                pass
            except Exception as e:
                logger.error(f"Error downloading track: {type(e).__name__}: {e}")

        results = await asyncio.gather(
            *[_download(t) for t in tracks], return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Album track processing error: {result}")

    async def postprocess(self):
        progress.remove_title(self.meta.album)


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
            logger.error(f"Error building album metadata for {id=}: {e}")
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
        todo = [track_id for track_id in tracklist if not self.db.downloaded(track_id)]
        done = len(tracklist) - len(todo)
        if tracklist and not todo:
            logger.info(f"{meta.album}: all {done} tracks already downloaded, skipping")
        elif done:
            logger.info(
                f"{meta.album}: skipping {done} of {len(tracklist)} tracks "
                "already downloaded"
            )
        folder = self.config.session.downloads.folder
        album_folder = self._album_folder(folder, meta)
        if tracklist and not todo:
            return Album(meta, [], self.config, album_folder, self.db)
        os.makedirs(album_folder, exist_ok=True)
        embed_cover, _ = await download_artwork(
            self.client.session,
            album_folder,
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
                folder=album_folder,
                db=self.db,
                cover_path=embed_cover,
            )
            for track_id in todo
        ]
        logger.debug("Pending tracks: %s", pending_tracks)
        return Album(meta, pending_tracks, self.config, album_folder, self.db)

    def _album_folder(self, parent: str, meta: AlbumMetadata) -> str:
        config = self.config.session
        if config.downloads.source_subdirectories:
            parent = os.path.join(parent, self.client.source.capitalize())
        formatter = config.filepaths.folder_format
        folder = clean_filepath(
            meta.format_folder_path(formatter), config.filepaths.restrict_characters
        )

        return os.path.join(parent, folder)
