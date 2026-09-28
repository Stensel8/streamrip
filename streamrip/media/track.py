import asyncio
import logging
import os
from dataclasses import dataclass

from .. import converter
from ..client import Client, Downloadable
from ..config import Config
from ..db import Database
from ..exceptions import NonStreamableError, TrackDownloadFailedError
from ..filepath_utils import clean_filename, fit_filename
from ..metadata import AlbumMetadata, Covers, TrackMetadata, tag_file
from ..metadata.tagger import TAGGABLE_EXTENSIONS
from ..progress import add_title, get_progress_callback, remove_title
from .artwork import download_artwork
from .media import Media, Pending
from .semaphore import global_download_semaphore

logger = logging.getLogger("streamrip")

# One try plus three retries with exponential backoff.
MAX_DOWNLOAD_ATTEMPTS = 4

# FLAC is always preferred over a lossy container for the same track: if one
# is already on disk, there's no reason to also keep (or fetch) the other.
LOSSLESS_EXTENSIONS = {"flac", "aiff", "aif"}
LOSSY_EXTENSIONS = {"m4a", "mp3"}


@dataclass(slots=True)
class Track(Media):
    meta: TrackMetadata
    downloadable: Downloadable
    config: Config
    folder: str
    # Is None if a cover doesn't exist for the track
    cover_path: str | None
    db: Database
    # change?
    download_path: str = ""
    is_single: bool = False
    # Base name (no extension) `download_path` is built from; kept around to
    # find a sibling file that only differs by extension.
    _path_stem: str = ""
    # Set in preprocess() when a lossless copy of this track already exists,
    # so download()/postprocess() skip it instead of fetching a redundant
    # lossy copy.
    _skip_lossy_duplicate: bool = False

    async def preprocess(self):
        self._set_download_path()
        os.makedirs(self.folder, exist_ok=True)
        if self._lossless_duplicate_exists():
            self._skip_lossy_duplicate = True
            logger.info(
                f"Skipping '{self.meta.title}': a lossless copy already exists, "
                f"not downloading the .{self.downloadable.extension} version."
            )
            return
        if self.is_single:
            add_title(self.meta.title)

    async def download(self):
        if self._skip_lossy_duplicate:
            return
        async with global_download_semaphore(self.config.session.downloads):
            for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
                # Retries continue the partial file instead of starting over,
                # so a connection that keeps dropping near the end of a large
                # FLAC still gets there (upstream #951, #1022).
                self.downloadable.resume = attempt > 1
                label = f"Track {self.meta.tracknumber}"
                if attempt > 1:
                    label += f" (retry {attempt - 1})"
                try:
                    with get_progress_callback(
                        self.config.session.cli.progress_bars,
                        await self.downloadable.size(),
                        label,
                    ) as callback:
                        await self.downloadable.download(self.download_path, callback)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    if attempt < MAX_DOWNLOAD_ATTEMPTS:
                        delay = 2**attempt  # 2s, 4s, 8s
                        logger.warning(
                            f"Error downloading track '{self.meta.title}', "
                            f"retrying in {delay}s: {error}"
                        )
                        await asyncio.sleep(delay)
                        continue

                    logger.error(
                        f"Persistent error downloading track '{self.meta.title}', "
                        f"skipping: {error}"
                    )
                    self.db.set_failed(
                        self.downloadable.source, "track", self.meta.info.id
                    )
                    if os.path.isfile(self.download_path):
                        os.remove(self.download_path)
                    # postprocess() normally does this, but raising below
                    # skips it, which would leave a phantom title in the
                    # progress display for the rest of the run.
                    if self.is_single:
                        remove_title(self.meta.title)
                    raise TrackDownloadFailedError(
                        f"{self.meta.title} ({self.meta.info.id})"
                    ) from e

    async def postprocess(self):
        if self._skip_lossy_duplicate:
            self.db.set_downloaded(self.meta.info.id)
            return

        if self.is_single:
            remove_title(self.meta.title)

        exclude = self.config.session.metadata.exclude
        await tag_file(self.download_path, self.meta, self.cover_path, exclude)
        if self.config.session.conversion.enabled:
            try:
                await self._convert()
            except Exception as e:
                # The download itself is fine and fully tagged; keep it and
                # still record it, instead of re-downloading it on every run
                # (upstream #1010, e.g. ffmpeg without libfdk_aac).
                logger.error(
                    f"Could not convert '{self.meta.title}', keeping the "
                    f"original file: {type(e).__name__}: {e}"
                )
        else:
            # Conversion picks its own target codec for every track, so a
            # mismatched sibling from a previous run doesn't mean anything
            # there; only clean up when this file's own format is the one on
            # disk.
            self._remove_lossy_duplicates()

        self.db.set_downloaded(self.meta.info.id)

    async def _convert(self):
        c = self.config.session.conversion
        engine_class = converter.get(c.codec)
        # Lossy codecs honour [conversion] lossy_bitrate (upstream #823).
        ffmpeg_arg = None
        if not engine_class.lossless:
            ffmpeg_arg = engine_class.get_quality_arg(c.lossy_bitrate)
        engine = engine_class(
            filename=self.download_path,
            ffmpeg_arg=ffmpeg_arg,
            sampling_rate=c.sampling_rate,
            bit_depth=c.bit_depth,
            remove_source=True,  # always going to delete the old file
        )
        await engine.convert()
        self.download_path = engine.final_fn  # because the extension changed
        # ffmpeg does not reliably carry every tag into the new container, so
        # re-tag the converted file where we know how to.
        if self.download_path.rsplit(".", 1)[-1].lower() in TAGGABLE_EXTENSIONS:
            await tag_file(
                self.download_path,
                self.meta,
                self.cover_path,
                self.config.session.metadata.exclude,
            )

    def _set_download_path(self):
        c = self.config.session.filepaths
        formatter = c.track_format
        track_path = clean_filename(
            self.meta.format_track_path(formatter),
            restrict=c.restrict_characters,
        )
        if c.truncate_to > 0 and len(track_path) > c.truncate_to:
            track_path = track_path[: c.truncate_to]

        self._path_stem = track_path
        self.download_path = os.path.join(
            self.folder,
            fit_filename(track_path, self.downloadable.extension),
        )

    def _sibling_path(self, extension: str) -> str:
        """Path this track would have on disk with a different extension."""
        return os.path.join(self.folder, fit_filename(self._path_stem, extension))

    def _lossless_duplicate_exists(self) -> bool:
        if self.downloadable.extension.lower() not in LOSSY_EXTENSIONS:
            return False
        return any(
            os.path.isfile(self._sibling_path(ext)) for ext in LOSSLESS_EXTENSIONS
        )

    def _remove_lossy_duplicates(self):
        final_ext = self.download_path.rsplit(".", 1)[-1].lower()
        if final_ext not in LOSSLESS_EXTENSIONS:
            return
        for ext in LOSSY_EXTENSIONS:
            sibling = self._sibling_path(ext)
            if os.path.isfile(sibling):
                logger.info(f"Removing lower-quality duplicate: {sibling}")
                os.remove(sibling)


@dataclass(slots=True)
class PendingTrack(Pending):
    id: str
    album: AlbumMetadata
    client: Client
    config: Config
    folder: str
    db: Database
    # cover_path is None <==> Artwork for this track doesn't exist in API
    cover_path: str | None

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        source = self.client.source
        # Every failure below has to be recorded, not just logged. An unlogged
        # failure leaves no trace anywhere: no file, no downloads.db row, and
        # nothing in the failed db for `streamrip repair` to retry -- the track just
        # silently goes missing from the album.
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Track {self.id} not available for stream on {source}: {e}")
            self.db.set_failed(source, "track", self.id)
            return None

        try:
            meta = TrackMetadata.from_resp(self.album, source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for {self.id}: {e}")
            self.db.set_failed(source, "track", self.id)
            return None

        if meta is None:
            logger.error(f"Track {self.id} not available for stream on {source}")
            self.db.set_failed(source, "track", self.id)
            return None

        quality = self.config.session.get_source(source).quality
        try:
            downloadable = await self.client.get_downloadable(self.id, quality)
        except NonStreamableError as e:
            logger.error(
                f"Error getting downloadable data for track {meta.tracknumber} [{self.id}]: {e}"
            )
            self.db.set_failed(source, "track", self.id)
            return None

        downloads_config = self.config.session.downloads
        if downloads_config.disc_subdirectories and self.album.disctotal > 1:
            folder = os.path.join(self.folder, f"Disc {meta.discnumber}")
        else:
            folder = self.folder

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            self.cover_path,
            self.db,
        )


@dataclass(slots=True)
class PendingSingle(Pending):
    """Whereas PendingTrack is used in the context of an album, where the album metadata
    and cover have been resolved, PendingSingle is used when a single track is downloaded.

    This resolves the Album metadata and downloads the cover to pass to the Track class.
    """

    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        # As in PendingTrack.resolve: record every failure, so a track that
        # dies here is retryable by `streamrip repair` instead of vanishing.
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Error fetching track {self.id}: {e}")
            self.db.set_failed(self.client.source, "track", self.id)
            return None
        # Patch for soundcloud
        try:
            album = AlbumMetadata.from_track_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for track {id=}: {e}")
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        if album is None:
            self.db.set_failed(self.client.source, "track", self.id)
            logger.error(
                f"Cannot stream track (am) ({self.id}) on {self.client.source}",
            )
            return None

        try:
            meta = TrackMetadata.from_resp(album, self.client.source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for track {id=}: {e}")
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        if meta is None:
            self.db.set_failed(self.client.source, "track", self.id)
            logger.error(
                f"Cannot stream track (tm) ({self.id}) on {self.client.source}",
            )
            return None

        config = self.config.session
        quality = getattr(config, self.client.source).quality
        assert isinstance(quality, int)
        parent = config.downloads.folder
        in_album_folder = config.filepaths.add_singles_to_folder
        if in_album_folder:
            album_folder = self._format_folder(album)
        else:
            album_folder = parent

        os.makedirs(album_folder, exist_ok=True)

        embedded_cover_path, downloadable = await asyncio.gather(
            self._download_cover(album.covers, album_folder),
            self.client.get_downloadable(self.id, quality),
        )

        # Mirror PendingTrack: a track belonging to a multi-disc album lives in
        # that album's disc subfolder. Without this, downloading one track of a
        # multi-disc album (`streamrip repair`, or any single-track URL) drops it
        # beside the Disc folders rather than into the one it belongs to.
        # Only meaningful when we're actually building the album's folder --
        # otherwise this would create a bare "Disc N" in the download root.
        folder = album_folder
        if (
            in_album_folder
            and config.downloads.disc_subdirectories
            and album.disctotal > 1
        ):
            folder = os.path.join(album_folder, f"Disc {meta.discnumber}")
            os.makedirs(folder, exist_ok=True)

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            embedded_cover_path,
            self.db,
            is_single=True,
        )

    def _format_folder(self, meta: AlbumMetadata) -> str:
        c = self.config.session
        parent = c.downloads.folder
        formatter = c.filepaths.folder_format
        if c.downloads.source_subdirectories:
            parent = os.path.join(parent, self.client.source.capitalize())

        return os.path.join(parent, meta.format_folder_path(formatter))

    async def _download_cover(self, covers: Covers, folder: str) -> str | None:
        embed_path, _ = await download_artwork(
            self.client.session,
            folder,
            covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        return embed_path
