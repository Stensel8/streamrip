import logging
import os
from enum import Enum

import aiofiles
from mutagen import id3
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,  # type: ignore
    ID3,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover

from .track import TrackMetadata

logger = logging.getLogger("streamrip")

FLAC_MAX_BLOCKSIZE = 16777215  # 16.7 MB

# Where each of streamrip's tag names (see _tag_values) goes in MP3/AIFF (ID3)
# and M4A files. FLAC's Vorbis comments take every name, upper-cased.
MP3_KEY = {
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
}

MP4_KEY = {
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
}


def _names(names: list[str] | None, joined: str):
    """Several artists as a real multi-valued tag, not one "A, B" string that
    players would have to split up again (and mostly don't).
    """
    return names if names and len(names) > 1 else joined


def _tag_values(meta: TrackMetadata) -> dict:
    """A track's tags by streamrip's own names, leaving out empty ones."""
    album = meta.album
    values = {
        "title": meta.title,
        "artist": _names(meta.artists, meta.artist),
        "album": album.album,
        "albumartist": _names(album.albumartists, album.albumartist),
        "composer": meta.composer,
        "year": album.year,
        "description": album.description,
        "genre": album.get_genres(),
        "lyrics": meta.lyrics,
        "copyright": album.get_copyright(),
        "compilation": album.compilation,
        "tracknumber": meta.tracknumber,
        "discnumber": meta.discnumber,
        "tracktotal": album.tracktotal,
        "disctotal": album.disctotal,
        "date": album.date,
        "isrc": meta.isrc,
        "version": album.version,
    }
    return {k: v for k, v in values.items() if v is not None and v != ""}


def _flac_value(name: str, value):
    """Format a tag value for a FLAC Vorbis comment."""
    if name in ("tracknumber", "discnumber", "tracktotal", "disctotal"):
        return f"{int(value):02}"
    return value if isinstance(value, list) else str(value)


def _mp3_value(name: str, value, values: dict):
    """Format a tag value for an ID3 (MP3/AIFF) frame."""
    total = {"tracknumber": "tracktotal", "discnumber": "disctotal"}.get(name)
    if total in values:
        return f"{value}/{values[total]}"
    # save_audio() writes ID3v2.3, which joins a list of artists with "/".
    return value if isinstance(value, list) else str(value)


def _mp4_value(name: str, value, values: dict):
    """Format a tag value for an MP4 (M4A) atom."""
    if name == "tracknumber":
        return [(value, values.get("tracktotal", 0))]
    if name == "discnumber":
        return [(value, values.get("disctotal", 0))]
    if name == "isrc":
        # A freeform atom, which mutagen wants as bytes.
        return value.encode("utf-8")
    if name == "compilation":
        return True  # cpil is a flag, and only ever set for compilations
    return value if isinstance(value, list) else str(value)


class Container(Enum):
    FLAC = 1
    AAC = 2
    MP3 = 3
    AIFF = 4

    def get_mutagen_class(self, path: str):
        if self == Container.FLAC:
            return FLAC(path)
        elif self == Container.AAC:
            return MP4(path)
        elif self == Container.MP3:
            try:
                return ID3(path)
            except ID3NoHeaderError:
                return ID3()
        elif self == Container.AIFF:
            audio = AIFF(path)
            if audio.tags is None:
                audio.add_tags()
            return audio.tags

    def get_tag_pairs(self, meta, exclude=()) -> list[tuple]:
        """Return this container's (key, value) tag pairs for meta."""
        # [metadata] exclude lists streamrip's own tag names ("genre",
        # "albumartist", ...); it used to be ignored entirely (upstream #850).
        values = {k: v for k, v in _tag_values(meta).items() if k not in exclude}
        if self == Container.FLAC:
            return [(k.upper(), _flac_value(k, v)) for k, v in values.items()]
        if self in (Container.MP3, Container.AIFF):
            return [
                (
                    frame.__name__,
                    frame(encoding=3, text=_mp3_value(k, values[k], values)),
                )
                for k, frame in MP3_KEY.items()
                if k in values
            ]
        if self == Container.AAC:
            return [
                (key, _mp4_value(k, values[k], values))
                for k, key in MP4_KEY.items()
                if k in values
            ]
        return []

    def tag_audio(self, audio, tags: list[tuple]):
        for k, v in tags:
            audio[k] = v

    async def embed_cover(self, audio, cover_path):
        """Embed the JPEG at cover_path as the front cover (picture type 3)."""
        if self == Container.FLAC and os.path.getsize(cover_path) > FLAC_MAX_BLOCKSIZE:
            raise Exception("Cover art too big for FLAC")
        async with aiofiles.open(cover_path, "rb") as img:
            data = await img.read()
        if self == Container.FLAC:
            cover = Picture()
            cover.type, cover.mime, cover.data = 3, "image/jpeg", data
            # add_picture appends, unlike ID3 and MP4 which replace: without
            # this, re-tagging a converted FLAC embedded a second cover.
            audio.clear_pictures()
            audio.add_picture(cover)
        elif self in (Container.MP3, Container.AIFF):
            audio.add(APIC(type=3, mime="image/jpeg", data=data))
        elif self == Container.AAC:
            audio["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]

    def save_audio(self, audio, path):
        """Write the tagged audio object back to path."""
        if self in (Container.FLAC, Container.AAC):
            audio.save()
        elif self == Container.MP3:
            # ID3v2.3 for the widest player support. This used to pass the
            # string "v2_version=3" as the v1 argument, so it never applied.
            audio.update_to_v23()
            audio.save(path, v2_version=3)
        elif self == Container.AIFF:
            audio.save(path)


EXTENSION_CONTAINERS = {
    "flac": Container.FLAC,
    "m4a": Container.AAC,
    "mp3": Container.MP3,
    "aiff": Container.AIFF,
    "aif": Container.AIFF,
}
# Extensions tag_file() can write to (others keep what ffmpeg copied).
TAGGABLE_EXTENSIONS = frozenset(EXTENSION_CONTAINERS)


async def tag_file(
    path: str,
    meta: TrackMetadata,
    cover_path: str | None,
    exclude: list[str] | tuple[str, ...] = (),
):
    ext = path.split(".")[-1].lower()
    container = EXTENSION_CONTAINERS.get(ext)
    if container is None:
        raise Exception(f"Invalid extension {ext}")

    audio = container.get_mutagen_class(path)
    tags = container.get_tag_pairs(meta, exclude)
    logger.debug("Tagging with %s", tags)
    container.tag_audio(audio, tags)
    if cover_path is not None and "cover" not in (exclude or ()):
        await container.embed_cover(audio, cover_path)
    container.save_audio(audio, path)
