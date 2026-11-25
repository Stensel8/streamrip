import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC

from .. import converter, lyrics
from ..client.audio_match import MatchTrack
from ..exceptions import (
    FFmpegNotFoundError,
    NonStreamableError,
    TrackDownloadFailedError,
)
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
from ..utils.ffmpeg_utils import measure_replaygain
from .artwork import download_artwork
from .media import Media, Pending, PendingItem
from .semaphore import global_download_semaphore
lazy from ..client import Client, Downloadable
lazy from ..config import Config
lazy from ..db import Database

logger = logging.getLogger("streamrip")

# One try plus three retries with exponential backoff.
MAX_DOWNLOAD_ATTEMPTS = 4

# A download url resolved this long ago is refreshed before use: sources sign
# their urls with a token whose real TTL isn't published, so this is a
# conservative margin, not a measured one. Every retry refreshes regardless,
# as a backstop if the margin guesses wrong.
STALE_DOWNLOADABLE_AFTER = 45

# Quality tiers 2 and up are lossless on every source that has tiers
# (Qobuz, Tidal, Deezer); a failing track never steps down below this.
LOWEST_LOSSLESS_TIER = 2
QUALITY_NAMES = {2: "16 bit, 44.1 kHz", 3: "24 bit", 4: "24 bit, up to 192 kHz"}

# Formats a track is downloaded in. A lossless copy of a track makes a lossy
# copy of the same track redundant, so only one of them is kept.
DEDUP_EXTENSIONS = ("flac", "m4a", "mp3")


def _open_audio(path: Path):
    """`path` as a mutagen file with format-independent tag names, or None."""
    try:
        return mutagen.File(path, easy=True)
    except mutagen.MutagenError, OSError:
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
    folder: Path
    # Is None if a cover doesn't exist for the track
    cover_path: Path | None
    db: Database
    # Asked for the track's lyrics after the download; see _look_up_lyrics.
    client: Client | None = None
    download_path: Path = Path()
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
            await super().rip()
        except Exception:
            self.db.set_failed(self.downloadable.source, "track", self.meta.info.id)
            if self.is_single:
                remove_title(id(self), self.config.session.cli.progress_bars)
            raise

    async def preprocess(self):
        """Set the download path and skip it if a lossless copy already exists."""
        self._set_download_path()
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.downloadable.extension != "flac" and self._copies_on_disk(
            lossless=True
        ):
            self._skip_lossy_duplicate = True
            logger.info(
                "Skipping '%s': a lossless copy already exists, not downloading the "
                ".%s version.",
                self.meta.title,
                self.downloadable.extension,
            )
            return
        if self.is_single:
            add_title(id(self), self.meta.title, self.config.session.cli.progress_bars)

    async def download(self):
        """Skip redundant lossy copies or download with progress and retries.

        Missing ffmpeg fails without retrying. A track that keeps failing at
        a lossless quality is tried once more one lossless step lower (#122:
        a Qobuz file that its CDN broke off after one byte, every time). On
        final failure, remove partial output and raise
        TrackDownloadFailedError, which rip() records.
        """
        if self._skip_lossy_duplicate:
            return
        tier = None  # the configured one
        while True:
            try:
                await self._download_attempts(tier)
            except TrackDownloadFailedError as e:
                lower = await self._lower_quality(e.__cause__)
                error = f"{type(e.__cause__).__name__}: {e.__cause__}"
                if lower is None:
                    logger.error(
                        "Persistent error downloading track '%s', skipping: %s",
                        self.meta.title,
                        error,
                    )
                    raise
                served, tier, self.downloadable = lower
                logger.warning(
                    "Persistent error downloading track '%s' at %s, downloading it "
                    "at %s instead: %s",
                    self.meta.title,
                    QUALITY_NAMES.get(served, f"quality {served}"),
                    QUALITY_NAMES.get(tier, f"quality {tier}"),
                    error,
                )
            else:
                return

    def _configured_tier(self) -> int | None:
        """The quality this track's source is set to download, if it has one."""
        if self.client is None:
            return None
        section = getattr(self.config.session, self.client.source, None)
        return getattr(section, "quality", None)

    async def _lower_quality(self, cause) -> tuple[int, int, Downloadable] | None:
        """The quality that failed, the next lossless one down and its file,
        when there is one.

        Never lossy: a FLAC that fails is not replaced with an MP3 or AAC.
        A quality the source answers with the very file that failed (Qobuz
        serves 24/96 for both 3 and 4 when there is no 24/192) is skipped.
        """
        if self.client is None or isinstance(cause, FFmpegNotFoundError):
            return None
        served = self.downloadable.quality
        if served is None:
            served = self._configured_tier()
        if served is None:
            return None
        for tier in range(served - 1, LOWEST_LOSSLESS_TIER - 1, -1):
            try:
                fresh = await self.client.get_downloadable(self.meta.info.id, tier)
            except Exception as e:
                logger.debug(
                    "No quality %s for '%s': %s: %s",
                    tier,
                    self.meta.title,
                    type(e).__name__,
                    e,
                )
                return None
            if fresh.extension != self.downloadable.extension:
                return None  # another container than download_path was made for
            if fresh.quality is not None and fresh.quality >= served:
                continue
            fresh.resolved_at = time.time()
            return served, tier, fresh
        return None

    async def _download_attempts(self, tier: int | None):
        """Download at `tier` (None: the configured quality), retrying.

        Raises TrackDownloadFailedError, from the last error, when every
        attempt failed; the part file is gone by then.
        """
        quality = format_quality(
            self.meta.album.info.container,
            self.meta.album.info.bit_depth,
            self.meta.album.info.sampling_rate,
        )
        # The spotify backend refused this track: re-downloading it on the
        # youtube backend only happens once, when _fallback_to_youtube does it.
        self._fallback_used = False
        # Downloaded beside the track, and only then put in its place: a retry
        # resumes this file, never a finished copy already at download_path
        # (with --no-db), and a download that fails leaves that copy alone.
        part = self.download_path.with_stem(f"{self.download_path.stem}.part")
        slots = global_download_semaphore(self.config.session.downloads)
        # Ends only in a return or a raise: never as a success without a file.
        attempt = 0
        while True:
            attempt += 1
            # The source's own line first (Spotify's audio plays one track at a
            # time): a track waiting in it holds no slot and shows no row.
            async with self.downloadable.queue, slots:
                # A url resolved well before its turn here (queued behind
                # max_connections) or reused across a retry can have gone
                # stale; get a fresh one rather than fail on a dead token.
                if (
                    self.client is not None
                    and self.downloadable.url_expires
                    and (
                        attempt > 1
                        or time.time() - self.downloadable.resolved_at
                        > STALE_DOWNLOADABLE_AFTER
                    )
                ):
                    try:
                        if tier is None:
                            tier_now = getattr(
                                self.config.session, self.client.source
                            ).quality
                        else:
                            tier_now = tier
                        fresh = await self.client.get_downloadable(
                            self.meta.info.id, tier_now
                        )
                        fresh.resolved_at = time.time()
                        self.downloadable = fresh
                    except Exception as e:
                        logger.debug(
                            "Could not refresh download url for '%s', reusing "
                            "the old one: %s: %s",
                            self.meta.title,
                            type(e).__name__,
                            e,
                        )
                # Retries continue the partial file instead of starting over,
                # so a connection that keeps dropping near the end of a large
                # FLAC still gets there (upstream #951, #1022).
                self.downloadable.resume = attempt > 1
                # Plain "Track N" rows don't say whose track it is -- which
                # matters for labels, and for features on an artist's page.
                label = (
                    f"{self.meta.album.albumartist} - "
                    f"Track {self.meta.tracknumber} "
                    f"{quality if tier is None else QUALITY_NAMES.get(tier, '')}"
                )
                if attempt > 1:
                    label += f" (retry {attempt - 1})"
                try:
                    with get_progress_callback(
                        self.config.session.cli.progress_bars,
                        await self.downloadable.size(),
                        label,
                    ) as callback:
                        await self.downloadable.download(part, callback)
                except asyncio.CancelledError:
                    # Ctrl-C. No later attempt continues this file (a new run
                    # starts over), and a "Song.part.flac" left beside the
                    # tracks is picked up by whatever indexes the library.
                    part.unlink(missing_ok=True)
                    raise
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    # The spotify backend refused the audio (e.g. Spotify's
                    # audio-key cutoff for new accounts): the track is not
                    # streamable there, but it may be on YouTube Music.
                    # Re-download it from there, once.
                    if self._fallback_to_youtube(e):
                        # Wait for the re-resolution: the next attempt must
                        # use the youtube backend's downloadable, not the one
                        # Spotify just refused.
                        failed = self.downloadable
                        await self._fallback_task
                        part.unlink(missing_ok=True)
                        if self.downloadable is failed:
                            # No YouTube Music audio either: Spotify's again
                            # would fail the same way, in real time.
                            raise TrackDownloadFailedError(
                                f"{self.meta.title} ({self.meta.info.id})"
                            ) from e
                        logger.warning(
                            "Error downloading track '%s' from Spotify, "
                            "downloading it from YouTube Music instead: %s",
                            self.meta.title,
                            error,
                        )
                        # The re-resolution may have renamed the file (the
                        # youtube backend's extension); the next attempt
                        # continues that file, not the one just refused.
                        part = self.download_path.with_stem(
                            f"{self.download_path.stem}.part"
                        )
                        # YouTube Music gets attempts of its own: a switch on
                        # the last one used to leave it none.
                        attempt = 0
                        continue
                    # A missing ffmpeg does not fix itself in 2-8 seconds.
                    if attempt >= MAX_DOWNLOAD_ATTEMPTS or isinstance(
                        e, FFmpegNotFoundError
                    ):
                        part.unlink(missing_ok=True)
                        raise TrackDownloadFailedError(
                            f"{self.meta.title} ({self.meta.info.id})"
                        ) from e
                    delay = 2**attempt  # 2s, 4s, 8s
                    logger.warning(
                        "Error downloading track '%s', retrying in %ss: %s: %s",
                        self.meta.title,
                        delay,
                        type(e).__name__,
                        e,
                    )
                else:
                    part.replace(self.download_path)
                    return
            # Outside the slot: a track waiting to retry leaves it to another.
            await asyncio.sleep(delay)

    def _fallback_to_youtube(self, error: Exception) -> bool:
        """Whether this track should be re-downloaded from YouTube Music.

        Only the spotify backend fails this way: Spotify refuses the audio
        (NonStreamableError), and the config still asks for the spotify
        backend, so the refusal is the account's, not a fluke. The re-fetch
        from the youtube backend happens once: after it, the track is no
        longer a spotify one, and the failure is recorded as usual.
        """
        if (
            not isinstance(error, NonStreamableError)
            or self._fallback_used
            or self.client is None
            or self.client.source != "spotify"
            or self.config.session.spotify.backend != "spotify"
        ):
            return False
        self._fallback_used = True
        self._fallback_task = asyncio.ensure_future(self._re_download_from_youtube())
        return True

    async def _re_download_from_youtube(self) -> None:
        """Swap this track's downloadable for the one the youtube backend
        resolves. The download loop continues with it on its next attempt."""
        try:
            # Not get_downloadable: that asks Soloist again unless Spotify
            # refused the track, and a sink or a busy session fails just the same.
            fresh = await self.client.youtube_downloadable(self.meta.info.id)
        except Exception as e:
            logger.warning(
                "Could not find '%s' on YouTube Music either: %s: %s",
                self.meta.title,
                type(e).__name__,
                e,
            )
            return
        fresh.resolved_at = time.time()
        self.downloadable = fresh
        # The refusal came from the spotify backend; the bytes are the
        # youtube backend's, so the name must be too: a "T.flac" that holds
        # an m4a is what makes the tagger fail.
        if self.download_path.suffix.lower().removeprefix(".") != fresh.extension:
            self.download_path = self.download_path.with_suffix(f".{fresh.extension}")

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
        await self._fill_replaygain()
        await tag_file(self.download_path, self.meta, self.cover_path, exclude)
        if self.config.session.conversion.enabled:
            try:
                await self._convert()
            except Exception as e:
                # The download itself is fine and fully tagged; keep it and
                # still record it, instead of re-downloading it on every run
                # (upstream #1010, e.g. ffmpeg without libfdk_aac).
                logger.error(
                    "Could not convert '%s', keeping the original file: %s: %s",
                    self.meta.title,
                    type(e).__name__,
                    e,
                )

        self._remove_lossy_copies()
        self.db.set_downloaded(self.downloadable.source, self.meta.info.id)

    async def _look_up_lyrics(self):
        """The track's lyrics from its source or else from LRCLIB.

        Unless the user turned lyrics off. Asked for after the download, so the
        download needs no request for them. LRCLIB is given the length of the
        file just downloaded, as synced lyrics only fit one edit of a song. A
        missing lyric never costs the track.
        """
        c = self.config.session
        if not c.downloads.lyrics or "lyrics" in c.metadata.exclude:
            return
        # An MP3 holds plain lyrics; the others the synced (LRC) ones.
        plain = self.download_path.suffix.lower() == ".mp3" or (
            c.conversion.enabled and c.conversion.codec.upper() == "MP3"
        )
        if self.client is not None:
            try:
                self.meta.lyrics = await self.client.get_lyrics(
                    self.meta.info.id, plain
                )
            except Exception as e:
                # Lyrics that should have been there: worth knowing about, never
                # worth the track (a 401 used to cost a whole album, #959).
                logger.warning(
                    "Could not get the lyrics of '%s' from %s: %s: %s",
                    self.meta.title,
                    self.client.source,
                    type(e).__name__,
                    e,
                )
            if self.meta.lyrics:
                self._save_lrc(plain)
                return
        try:
            audio = _open_audio(self.download_path)
            seconds = audio.info.length if audio is not None else None
            wanted = MatchTrack(
                self.meta.title,
                self.meta.artists or [self.meta.artist],
                self.meta.album.album,
                int(seconds * 1000) if seconds else None,
            )
            found = await lyrics.find_lyrics(
                wanted, seconds, plain, c.downloads.verify_ssl
            )
        except lyrics.LyricsUnavailableError as e:
            # Not "no lyrics found": LRCLIB did not answer. It is said once, in
            # a warning, when it is left alone, and not for every track.
            logger.debug("Lyrics of '%s' not looked up: %s", self.meta.title, e)
            return
        except Exception as e:
            logger.warning(
                "Could not look up lyrics for '%s': %s: %s",
                self.meta.title,
                type(e).__name__,
                e,
            )
            return
        if found:
            self.meta.lyrics = found
            self._save_lrc(plain)
        else:
            logger.info("No lyrics found for '%s' on LRCLIB", self.meta.title)

    def _save_lrc(self, plain: bool):
        """Write the looked-up lyrics to an .lrc file beside the track too.

        For a player that does not read the embedded tag. Skipped for plain
        (MP3) lyrics, which have no timestamps to make an .lrc file worth it.
        """
        if plain or not self.config.session.downloads.save_lrc:
            return
        self._sibling_path("lrc").write_text(self.meta.lyrics)

    async def _fill_replaygain(self):
        """The ReplayGain of the track, measured here for every source alike
        (-18 LUFS, as Qobuz gives it): Deezer, Spotify and SoundCloud give none,
        and Tidal gives one value for a whole album. What the source gave stays
        when ffmpeg cannot measure."""
        meta = self.meta
        wanted = {"replaygain_track_gain", "replaygain_track_peak"}
        if wanted <= set(self.config.session.metadata.exclude):
            return
        try:
            measured = await measure_replaygain(self.download_path)
        except Exception as e:  # no ReplayGain is no reason to lose the track
            logger.debug("Could not measure the ReplayGain of '%s': %s", meta.title, e)
            return
        if measured is not None:
            meta.replaygain_gain, meta.replaygain_peak = measured

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
        if self.download_path.suffix.lower().removeprefix(".") in TAGGABLE_EXTENSIONS:
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
            self.meta.format_track_path(formatter, c.restrict_characters),
            restrict=c.restrict_characters,
        )
        if c.truncate_to > 0 and len(track_path) > c.truncate_to:
            track_path = track_path[: c.truncate_to]

        self._path_stem = track_path
        self.download_path = self.folder / fit_filename(
            track_path, self.downloadable.extension
        )

    def _sibling_path(self, extension: str) -> Path:
        """Path this track would have on disk with a different extension."""
        return self.folder / fit_filename(self._path_stem, extension)

    def _copies_on_disk(self, lossless: bool) -> list[Path]:
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
            if path == self.download_path or not path.is_file():
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
            logger.info("Removing lower-quality copy: %s", path)
            path.unlink()


def album_folder(config: Config, source: str, album: AlbumMetadata) -> Path:
    """The folder an album's tracks go in, inside the downloads folder.

    Raises ValueError if it would be anywhere else: the names in it come from
    a streaming service. With `create_album_folder` off, this is shared by
    every album, so its tracks must already have unique names of their own.
    """
    c = config.session
    root = Path(c.downloads.folder)
    parent = root
    if c.downloads.source_subdirectories:
        parent = parent / source.capitalize()
    if not c.downloads.create_album_folder:
        # No album/artist-derived component is added below, so there is
        # nothing for ensure_inside to validate; `parent` (root, or
        # root/Source) is already known-safe.
        return parent
    folder = album.format_folder_path(
        c.filepaths.folder_format, c.filepaths.restrict_characters
    )
    return ensure_inside(
        root,
        parent / clean_filepath(folder.as_posix(), c.filepaths.restrict_characters),
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
        logger.info(
            "Skipping track %s. Marked as downloaded in the database.", track_id
        )
        db.skipped_now += 1
        return None
    try:
        resp = await client.get_metadata(track_id, "track")
        album = album or AlbumMetadata.from_track_resp(resp, source)
        meta = TrackMetadata.from_resp(album, source, resp) if album else None
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
    quality = getattr(config.session, client.source).quality
    # The stream is the request every track costs (its metadata can come with
    # its album's listing), and its download follows at once: so a track waits
    # its turn at the pace here. A skipped track never gets here.
    await client.before_track()
    try:
        downloadable = await client.get_downloadable(track_id, quality)
    except Exception as e:
        _record_failure(
            db,
            client.source,
            track_id,
            f"Error fetching download info for track {track_id}: {type(e).__name__}: {e}",
        )
        return None
    else:
        if downloadable is not None:
            downloadable.resolved_at = time.time()
        return downloadable


@dataclass(slots=True)
class PendingTrack(Pending):
    """A track of an album whose metadata and cover are already there."""

    is_track = True

    id: str
    album: AlbumMetadata
    client: Client
    config: Config
    folder: Path
    db: Database
    # cover_path is None <==> Artwork for this track doesn't exist in API
    cover_path: Path | None
    meta: TrackMetadata | None = None

    async def look_up(self) -> TrackMetadata | None:
        """This track's metadata, fetched once; None if it's not to download."""
        if self.meta is None:
            self.meta = await fetch_track_meta(
                self.client, self.db, self.id, self.album
            )
        return self.meta

    async def resolve(self) -> Track | None:
        """Fetch this track's metadata and download info, resolving into a Track."""
        meta = await self.look_up()
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
            folder = folder / f"Disc {meta.discnumber}"
        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            self.cover_path,
            self.db,
            self.client,
        )


@dataclass(slots=True)
class PendingSingle(PendingItem):
    """A track downloaded on its own, which brings its album's metadata and
    cover with it.
    """

    is_track = True

    async def resolve(self) -> Track | None:
        """Fetch this single's metadata, album info, and cover, into a Track."""
        meta = await fetch_track_meta(self.client, self.db, self.id)
        if meta is None:
            return None
        album, c = meta.album, self.config.session
        album.at_quality(getattr(c, self.client.source).quality, self.client.source)
        in_album_folder = (
            c.filepaths.add_singles_to_folder and c.downloads.create_album_folder
        )
        if in_album_folder:
            folder = album_folder(self.config, self.client.source, album)
        else:
            folder = Path(c.downloads.folder)
        folder.mkdir(parents=True, exist_ok=True)

        (cover_path, _), downloadable = await asyncio.gather(
            download_artwork(
                self.client.session,
                folder,
                album.covers,
                c.artwork,
                shared_folder=not in_album_folder,
            ),
            fetch_downloadable(self.client, self.config, self.db, self.id),
        )
        if downloadable is None:
            return None

        # As in an album download, a track of a multi-disc album goes in its
        # disc's subfolder (`streamrip repair` downloads singles too). Only
        # when it's in the album's folder, not as a bare "Disc N" in the root.
        if in_album_folder and c.downloads.disc_subdirectories and album.disctotal > 1:
            folder = folder / f"Disc {meta.discnumber}"
            folder.mkdir(parents=True, exist_ok=True)

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            cover_path,
            self.db,
            self.client,
            is_single=True,
        )
