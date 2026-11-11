import asyncio
import io
import logging
from enum import Enum
lazy from pathlib import Path

from mutagen import id3
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,  # type: ignore
    ID3,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover
lazy from PIL import Image

lazy from .track import TrackMetadata

logger = logging.getLogger("streamrip")

FLAC_MAX_BLOCKSIZE = 16777215  # 16.7 MB

# Where each of streamrip's tag names (see _tag_values) goes in MP3/AIFF (ID3)
# and M4A files, as MusicBrainz Picard writes them; a string in MP3_KEY is the
# description of a TXXX frame. FLAC's Vorbis comments take every name, upper-cased.
MP3_KEY = frozendict(
    {
        "title": id3.TIT2,
        "artist": id3.TPE1,
        "album": id3.TALB,
        "albumartist": id3.TPE2,
        "composer": id3.TCOM,
        "year": id3.TYER,
        "genre": id3.TCON,
        "lyrics": id3.USLT,
        "copyright": id3.TCOP,
        "compilation": id3.TCMP,
        "tracknumber": id3.TRCK,
        "discnumber": id3.TPOS,
        "isrc": id3.TSRC,
        "label": id3.TPUB,
        "barcode": "BARCODE",
        "releasetype": "MusicBrainz Album Type",
        "lyricist": id3.TEXT,
        "bpm": id3.TBPM,
        "initialkey": id3.TKEY,
        "itunesadvisory": "ITUNESADVISORY",
        "replaygain_track_gain": "REPLAYGAIN_TRACK_GAIN",
        "replaygain_track_peak": "REPLAYGAIN_TRACK_PEAK",
    }
)

MP4_KEY = frozendict(
    {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album": "\xa9alb",
        "albumartist": "aART",
        "composer": "\xa9wrt",
        "year": "\xa9day",
        "description": "desc",
        "genre": "\xa9gen",
        "lyrics": "\xa9lyr",
        "copyright": "cprt",
        "compilation": "cpil",
        "tracknumber": "trkn",
        "discnumber": "disk",
        "isrc": "----:com.apple.iTunes:ISRC",
        "label": "----:com.apple.iTunes:LABEL",
        "barcode": "----:com.apple.iTunes:BARCODE",
        "releasetype": "----:com.apple.iTunes:MusicBrainz Album Type",
        "lyricist": "----:com.apple.iTunes:LYRICIST",
        "bpm": "tmpo",
        "initialkey": "----:com.apple.iTunes:initialkey",
        "itunesadvisory": "rtng",
        "replaygain_track_gain": "----:com.apple.iTunes:replaygain_track_gain",
        "replaygain_track_peak": "----:com.apple.iTunes:replaygain_track_peak",
    }
)


def _names(names: list[str] | None, joined: str):
    """Several artists as a real multi-valued tag, not one "A, B" string that
    players would have to split up again (and mostly don't).
    """
    return names if names and len(names) > 1 else joined


def _total(total: int | None, number: int | None) -> int | None:
    """A track or disc count to tag, or None where it is not known: a 0, or
    the 1 a source's fallback guessed for track 5 ("5/1")."""
    return total if total and total >= (number or 0) else None


def _tag_values(meta: TrackMetadata) -> dict:
    """A track's tags by streamrip's own names, leaving out empty ones."""
    album = meta.album
    values = {
        "title": meta.title,
        "artist": _names(meta.artists, meta.artist),
        "album": album.album,
        "albumartist": _names(album.albumartists, album.albumartist),
        "composer": _names(meta.composers, meta.composer),
        # "Unknown" names a folder; it is no year.
        "year": None if album.year == "Unknown" else album.year,
        "description": album.description,
        "genre": album.get_genres(),
        "lyrics": meta.lyrics,
        "copyright": album.get_copyright(),
        "compilation": album.compilation,
        "tracknumber": meta.tracknumber,
        "discnumber": meta.discnumber,
        "tracktotal": _total(album.tracktotal, meta.tracknumber),
        "disctotal": _total(album.disctotal, meta.discnumber),
        "date": album.date,
        "isrc": meta.isrc,
        "version": album.version,
        "label": album.info.label,
        "barcode": album.barcode,
        "releasetype": album.releasetype,
        "lyricist": _names(meta.lyricists, ", ".join(meta.lyricists or [])),
        "bpm": meta.bpm,
        "initialkey": meta.key,
        # 1 is explicit; a clean edition (2) is not told apart from any other.
        "itunesadvisory": 1 if meta.info.explicit else None,
        "replaygain_track_gain": None
        if meta.replaygain_gain is None
        else f"{meta.replaygain_gain:.2f} dB",
        "replaygain_track_peak": None
        if meta.replaygain_peak is None
        else f"{meta.replaygain_peak:.6f}",
    }
    return {k: v for k, v in values.items() if v is not None and v != ""}


def _flac_value(name: str, value):
    """Format a tag value for a FLAC Vorbis comment."""
    if name in {"tracknumber", "discnumber", "tracktotal", "disctotal"}:
        return f"{int(value):02}"
    return value if isinstance(value, list) else str(value)


def _mp3_value(name: str, value, values: dict):
    """Format a tag value for an ID3 (MP3/AIFF) frame."""
    total = {"tracknumber": "tracktotal", "discnumber": "disctotal"}.get(name)
    if total in values:
        return f"{value}/{values[total]}"
    # save_audio() writes ID3v2.3, which joins a list of artists with "/".
    return value if isinstance(value, list) else str(value)


def _id3_frame(frame, text) -> tuple:
    """(key, frame) for an ID3 frame class, or a TXXX frame with that description."""
    if isinstance(frame, str):
        return f"TXXX:{frame}", id3.TXXX(encoding=3, desc=frame, text=text)
    return frame.__name__, frame(encoding=3, text=text)


def _mp4_value(name: str, value, values: dict):
    """Format a tag value for an MP4 (M4A) atom."""
    if name == "tracknumber":
        return [(value, values.get("tracktotal", 0))]
    if name == "discnumber":
        return [(value, values.get("disctotal", 0))]
    if name == "compilation":
        return True  # cpil is a flag, and only ever set for compilations
    if name in {"bpm", "itunesadvisory"}:
        return [int(value)]
    if MP4_KEY[name].startswith("----:"):
        # A freeform atom, which mutagen wants as bytes.
        items = value if isinstance(value, list) else [value]
        return [str(item).encode("utf-8") for item in items]
    return value if isinstance(value, list) else str(value)


class Container(Enum):
    FLAC = 1
    AAC = 2
    MP3 = 3
    AIFF = 4

    def get_mutagen_class(self, path: Path):
        """Open the file with this container's mutagen class.

        A file with no tags yet gets empty ones.
        """
        match self:
            case Container.FLAC:
                return FLAC(path)
            case Container.AAC:
                return MP4(path)
            case Container.MP3:
                try:
                    return ID3(path)
                except ID3NoHeaderError:
                    return ID3()
            case Container.AIFF:
                audio = AIFF(path)
                if audio.tags is None:
                    audio.add_tags()
                return audio.tags

    def get_tag_pairs(self, meta, exclude=()) -> list[tuple]:
        """Return this container's (key, value) tag pairs for meta."""
        # [metadata] exclude lists streamrip's own tag names ("genre",
        # "albumartist", ...); it used to be ignored entirely (upstream #850).
        values = {k: v for k, v in _tag_values(meta).items() if k not in exclude}
        match self:
            case Container.FLAC:
                return [(k.upper(), _flac_value(k, v)) for k, v in values.items()]
            case Container.MP3 | Container.AIFF:
                return [
                    _id3_frame(frame, _mp3_value(k, values[k], values))
                    for k, frame in MP3_KEY.items()
                    if k in values
                ]
            case Container.AAC:
                return [
                    (key, _mp4_value(k, values[k], values))
                    for k, key in MP4_KEY.items()
                    if k in values
                ]

    def clear_tags(self, audio):
        """Drop whatever tags are already there (e.g. an "encoder" comment an
        ffmpeg remux left behind) before this container's own set replaces them.
        """
        match self:
            case Container.FLAC | Container.AAC:
                # A file with no tag block at all (the FLACs from Tidal and
                # Qobuz in issue #91) has none to clear: tag_audio adds one.
                if audio.tags is not None:
                    audio.tags.clear()
            case Container.MP3 | Container.AIFF:
                audio.clear()

    def tag_audio(self, audio, tags: list[tuple]):
        """Set each (key, value) tag on the mutagen object."""
        for k, v in tags:
            audio[k] = v

    def embed_cover(self, audio, cover_path: Path):
        """Embed the JPEG at cover_path as the front cover (picture type 3)."""
        if self == Container.FLAC and cover_path.stat().st_size > FLAC_MAX_BLOCKSIZE:
            raise ValueError("Cover art too big for FLAC")
        data = cover_path.read_bytes()
        match self:
            case Container.FLAC:
                cover = Picture()
                cover.type, cover.mime, cover.data = 3, "image/jpeg", data
                # Left at Mutagen's default of 0 otherwise, which some players
                # take as "no usable cover" without decoding the JPEG itself
                # to find out.
                with Image.open(io.BytesIO(data)) as image:
                    cover.width, cover.height = image.size
                    cover.depth = 8 * len(image.getbands())
                # add_picture appends, unlike ID3 and MP4 which replace: without
                # this, re-tagging a converted FLAC embedded a second cover.
                audio.clear_pictures()
                audio.add_picture(cover)
            case Container.MP3 | Container.AIFF:
                audio.add(APIC(type=3, mime="image/jpeg", data=data))
            case Container.AAC:
                audio["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]

    def save_audio(self, audio, path):
        """Write the tagged audio object back to path."""
        match self:
            case Container.FLAC | Container.AAC:
                audio.save()
            case Container.MP3:
                # ID3v2.3 for the widest player support. This used to pass the
                # string "v2_version=3" as the v1 argument, so it never applied.
                audio.update_to_v23()
                audio.save(path, v2_version=3)
            case Container.AIFF:
                audio.save(path)


EXTENSION_CONTAINERS = frozendict(
    {
        "flac": Container.FLAC,
        "m4a": Container.AAC,
        "mp3": Container.MP3,
        "aiff": Container.AIFF,
        "aif": Container.AIFF,
    }
)
# Extensions tag_file() can write to (others keep what ffmpeg copied).
TAGGABLE_EXTENSIONS = frozenset(EXTENSION_CONTAINERS)


async def tag_file(
    path: Path,
    meta: TrackMetadata,
    cover_path: Path | None,
    exclude: list[str] | tuple[str, ...] = (),
):
    """Tag the file at path, in a thread: tags that outgrow their room (a cover
    into a FLAC with no tag block) make mutagen rewrite the whole file, which
    for a hi-res FLAC would hold up every other download meanwhile."""
    await asyncio.to_thread(_tag_file, path, meta, cover_path, exclude)


def _tag_file(path: Path, meta: TrackMetadata, cover_path: Path | None, exclude):
    ext = path.suffix.lower().removeprefix(".")
    container = EXTENSION_CONTAINERS.get(ext)
    if container is None:
        raise ValueError(f"Cannot tag .{ext} files")

    audio = container.get_mutagen_class(path)
    tags = container.get_tag_pairs(meta, exclude)
    logger.debug("Tagging with %s", tags)
    container.clear_tags(audio)
    container.tag_audio(audio, tags)
    if cover_path is not None and "cover" not in (exclude or ()):
        container.embed_cover(audio, cover_path)
    container.save_audio(audio, path)
