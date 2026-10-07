import asyncio
import logging
import os
from dataclasses import dataclass

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC

from .. import converter, lyrics
from ..client import Client, Downloadable
from ..client.audio_match import MatchTrack
from ..config import Config
from ..db import Database
from ..exceptions import FFmpegNotFoundError, TrackDownloadFailedError
from ..filepath_utils import (
    clean_filename,
    clean_filepath,
    ensure_inside,
    fit_filename,
)
from ..metadata import AlbumMetadata, TrackMetadata, tag_file
from ..metadata.tagger import TAGGABLE_EXTENSIONS
from ..metadata.util import format_quality
from ..progress import add_title, get_progress_callback, remove_title
from .artwork import download_artwork
from .media import Media, Pending
from .semaphore import global_download_semaphore

logger = logging.getLogger("streamrip")

# One try plus three retries with exponential backoff.
MAX_DOWNLOAD_ATTEMPTS = 4

# Formats a track is downloaded in. A lossless copy of a track makes a lossy
# copy of the same track redundant, so only one of them is kept.
DEDUP_EXTENSIONS = ("flac", "m4a", "mp3")


def _open_audio(path: str):
    """`path` as a mutagen file with format-independent tag names, or None."""
    try:
        return mutagen.File(path, easy=True)
    except Exception:
        return None


def _is_lossless(audio) -> bool:
    """Whether a mutagen file is a lossless (FLAC, AIFF, or ALAC) codec."""
    # ALAC and AAC share the .m4a extension, so this has to look at the codec.
    return isinstance(audio, FLAC | AIFF) or getattr(audio.info, "codec", "") == "alac"


def _first_tag(audio, key: str) -> str:
    """Return the first value of a tag, normalized for comparison."""
    return str((audio.tags.get(key) or [""])[0]).strip().casefold()


@dataclass(slots=True)
class Track(Media):
    meta: TrackMetadata
    downloadable: Downloadable
    config: Config
    folder: str
    # Is None if a cover doesn't exist for the track
    cover_path: str | None
    db: Database
    download_path: str = ""
    is_single: bool = False
    # Base name (no extension) `download_path` is built from; kept around to
    # find a sibling file that only differs by extension.
    _path_stem: str = ""
    # Set in preprocess() when a lossless copy of this track already exists,
    # so download()/postprocess() skip it instead of fetching a redundant
    # lossy copy.
    _skip_lossy_duplicate: bool = False

    async def rip(self):
        """Record failures from every processing phase for summaries and repair."""
        try:
            await super(Track, self).rip()
        except TrackDownloadFailedError:
            raise  # The exhausted download was already recorded.
        except Exception:
            self.db.set_failed(self.downloadable.source, "track", self.meta.info.id)
            if self.is_single:
                remove_title(id(self), self.config.session.cli.progress_bars)
            raise

    async def preprocess(self):
        """Set the download path and skip it if a lossless copy already exists."""
        self._set_download_path()
        os.makedirs(self.folder, exist_ok=True)
        if self.downloadable.extension != "flac" and self._copies_on_disk(
            lossless=True
        ):
            self._skip_lossy_duplicate = True
            logger.info(
                f"Skipping '{self.meta.title}': a lossless copy already exists, "
                f"not downloading the .{self.downloadable.extension} version."
            )
            return
        if self.is_single:
            add_title(id(self), self.meta.title, self.config.session.cli.progress_bars)

    async def download(self):
        """Skip redundant lossy copies or download with progress and retries.

        Missing ffmpeg fails without retrying. On final failure, record the
        failure, remove partial output, and raise TrackDownloadFailedError.
        """
        if self._skip_lossy_duplicate:
            return
        quality = format_quality(
            self.meta.album.info.container,
            self.meta.album.info.bit_depth,
            self.meta.album.info.sampling_rate,
        )
        async with global_download_semaphore(self.config.session.downloads):
            for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
                # Retries continue the partial file instead of starting over,
                # so a connection that keeps dropping near the end of a large
                # FLAC still gets there (upstream #951, #1022).
                self.downloadable.resume = attempt > 1
                # Plain "Track N" rows don't say whose track it is -- which
                # matters for labels, and for features on an artist's page.
                label = (
                    f"{self.meta.album.albumartist} - "
                    f"Track {self.meta.tracknumber} {quality}"
                )
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
                    # A missing ffmpeg does not fix itself in 2-8 seconds.
                    if attempt < MAX_DOWNLOAD_ATTEMPTS and not isinstance(
                        e, FFmpegNotFoundError
                    ):
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
                    if os.path.isfile(self.download_path):
                        os.remove(self.download_path)
                    # postprocess() normally does this, but raising below
                    # skips it, which would leave a phantom title in the
                    # progress display for the rest of the run.
                    if self.is_single:
                        remove_title(id(self), self.config.session.cli.progress_bars)
                    self.db.set_failed(
                        self.downloadable.source, "track", self.meta.info.id
                    )
                    raise TrackDownloadFailedError(
                        f"{self.meta.title} ({self.meta.info.id})"
                    ) from e

    async def postprocess(self):
        """Tag, convert, and dedup the downloaded file, then mark it downloaded."""
        if self._skip_lossy_duplicate:
            self.db.set_downloaded(
                self.downloadable.source, self.meta.info.id, new=False
            )
            self.db.skipped_now += 1
            return

        if self.is_single:
            remove_title(id(self), self.config.session.cli.progress_bars)

        exclude = self.config.session.metadata.exclude
        await self._look_up_lyrics()
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

        self._remove_lossy_copies()
        self.db.set_downloaded(self.downloadable.source, self.meta.info.id)

    async def _look_up_lyrics(self):
        """Look up on LRCLIB the lyrics of a track its source sent none for.

        Only when the user asked for it (`lyrics_fallback`). The length of the
        file just downloaded is used, as synced lyrics only fit one edit of a
        song. A missing lyric never costs the track.
        """
        c = self.config.session
        if (
            self.meta.lyrics
            or not (c.downloads.lyrics and c.downloads.lyrics_fallback)
            or "lyrics" in c.metadata.exclude
        ):
            return
        try:
            audio = _open_audio(self.download_path)
            seconds = audio.info.length if audio is not None else None
            # An MP3 holds plain lyrics; the others the synced (LRC) ones.
            plain = self.download_path.lower().endswith(".mp3") or (
                c.conversion.enabled and c.conversion.codec.upper() == "MP3"
            )
            wanted = MatchTrack(
                self.meta.title,
                self.meta.artists or [self.meta.artist],
                self.meta.album.album,
                int(seconds * 1000) if seconds else None,
            )
            found = await lyrics.find_lyrics(
                wanted, seconds, plain, c.downloads.verify_ssl
            )
        except Exception as e:
            logger.warning(
                f"Could not look up lyrics for '{self.meta.title}': "
                f"{type(e).__name__}: {e}"
            )
            return
        if found:
            self.meta.lyrics = found

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

    def _copies_on_disk(self, lossless: bool) -> list[str]:
        """Lossless (or lossy) files of this same track under another extension.

        A matching filename stem alone doesn't prove it's the same track: a
        single and an album can share one. So a file only counts if its title
        and album tags match this track's too.
        """
        want = (
            self.meta.title.strip().casefold(),
            self.meta.album.album.strip().casefold(),
        )
        copies = []
        for ext in DEDUP_EXTENSIONS:
            path = self._sibling_path(ext)
            if path == self.download_path or not os.path.isfile(path):
                continue
            audio = _open_audio(path)
            if (
                audio is not None
                and audio.tags
                and _is_lossless(audio) == lossless
                and (_first_tag(audio, "title"), _first_tag(audio, "album")) == want
            ):
                copies.append(path)
        return copies

    def _remove_lossy_copies(self):
        """Delete lossy copies of this track once a lossless copy has landed."""
        # A lossy download converted to FLAC is no better than the copy it would
        # replace; only a genuinely lossless download supersedes the others.
        if self.downloadable.extension != "flac":
            return
        final = _open_audio(self.download_path)
        if final is None or not _is_lossless(final):
            return
        for path in self._copies_on_disk(lossless=False):
            logger.info(f"Removing lower-quality copy: {path}")
            os.remove(path)


def album_folder(config: Config, source: str, album: AlbumMetadata) -> str:
    """The folder an album's tracks go in, inside the downloads folder.

    Raises ValueError if it would be anywhere else: the names in it come from
    a streaming service.
    """
    c = config.session
    parent = c.downloads.folder
    if c.downloads.source_subdirectories:
        parent = os.path.join(parent, source.capitalize())
    folder = album.format_folder_path(c.filepaths.folder_format)
    return ensure_inside(
        c.downloads.folder,
        os.path.join(parent, clean_filepath(folder, c.filepaths.restrict_characters)),
    )


def _record_failure(db: Database, source: str, track_id: str, message: str):
    """Log message and record the track as failed in the database."""
    # Every failure has to be recorded, not just logged: otherwise the track
    # silently goes missing, with nothing for `streamrip repair` to retry.
    logger.error(message)
    db.set_failed(source, "track", track_id)


async def fetch_track_meta(
    client: Client, db: Database, track_id: str, album: AlbumMetadata | None = None
) -> TrackMetadata | None:
    """A track's metadata, or None if it's already downloaded or unavailable.

    Without `album`, the album's metadata is read from the track's.
    """
    source = client.source
    if db.downloaded(source, track_id):
        logger.info(f"Skipping track {track_id}. Marked as downloaded in the database.")
        db.skipped_now += 1
        return None
    try:
        resp = await client.get_metadata(track_id, "track")
        album = album or AlbumMetadata.from_track_resp(resp, source)
        meta = album and TrackMetadata.from_resp(album, source, resp)
        # Disc numbers become directory components in both album and single
        # downloads. Reject malformed provider values before any filesystem I/O.
        if meta is not None and (
            type(meta.discnumber) is not int or meta.discnumber < 1
        ):
            raise ValueError("Disc number must be a positive integer")
    except Exception as e:
        _record_failure(
            db,
            source,
            track_id,
            f"Error fetching track {track_id}: {type(e).__name__}: {e}",
        )
        return None
    if meta is None:
        _record_failure(
            db,
            source,
            track_id,
            f"Track {track_id} not available for stream on {source}",
        )
    return meta


async def fetch_downloadable(
    client: Client, config: Config, db: Database, track_id: str
) -> Downloadable | None:
    """Return a Downloadable for the track, or None and record the failure."""
    quality = config.session.get_source(client.source).quality
    try:
        return await client.get_downloadable(track_id, quality)
    except Exception as e:
        _record_failure(
            db,
            client.source,
            track_id,
            f"Error fetching download info for track {track_id}: {type(e).__name__}: {e}",
        )
        return None


@dataclass(slots=True)
class PendingTrack(Pending):
    """A track of an album whose metadata and cover are already there."""

    id: str
    album: AlbumMetadata
    client: Client
    config: Config
    folder: str
    db: Database
    # cover_path is None <==> Artwork for this track doesn't exist in API
    cover_path: str | None

    async def resolve(self) -> Track | None:
        """Fetch this track's metadata and download info, resolving into a Track."""
        meta = await fetch_track_meta(self.client, self.db, self.id, self.album)
        if meta is None:
            return None
        downloadable = await fetch_downloadable(
            self.client, self.config, self.db, self.id
        )
        if downloadable is None:
            return None
        folder = self.folder
        if (
            self.config.session.downloads.disc_subdirectories
            and self.album.disctotal > 1
        ):
            folder = os.path.join(folder, f"Disc {meta.discnumber}")
        return Track(meta, downloadable, self.config, folder, self.cover_path, self.db)


@dataclass(slots=True)
class PendingSingle(Pending):
    """A track downloaded on its own, which brings its album's metadata and
    cover with it.
    """

    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Track | None:
        """Fetch this single's metadata, album info, and cover, into a Track."""
        meta = await fetch_track_meta(self.client, self.db, self.id)
        if meta is None:
            return None
        album, c = meta.album, self.config.session
        in_album_folder = c.filepaths.add_singles_to_folder
        if in_album_folder:
            folder = album_folder(self.config, self.client.source, album)
        else:
            folder = c.downloads.folder
        os.makedirs(folder, exist_ok=True)

        (cover_path, _), downloadable = await asyncio.gather(
            download_artwork(
                self.client.session, folder, album.covers, c.artwork, for_playlist=False
            ),
            fetch_downloadable(self.client, self.config, self.db, self.id),
        )
        if downloadable is None:
            return None

        # As in an album download, a track of a multi-disc album goes in its
        # disc's subfolder (`streamrip repair` downloads singles too). Only
        # when it's in the album's folder, not as a bare "Disc N" in the root.
        if in_album_folder and c.downloads.disc_subdirectories and album.disctotal > 1:
            folder = os.path.join(folder, f"Disc {meta.discnumber}")
            os.makedirs(folder, exist_ok=True)

        return Track(
            meta, downloadable, self.config, folder, cover_path, self.db, is_single=True
        )
