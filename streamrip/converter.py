"""Wrapper classes over FFMPEG."""

import asyncio
import base64
import functools
import logging
import os
import shutil
import subprocess
import uuid
from tempfile import gettempdir
from typing import Final

from .exceptions import ConversionError
from .utils.ffmpeg_utils import find_ffmpeg

logger = logging.getLogger("streamrip")

SAMPLING_RATES = {44100, 48000, 88200, 96000, 176400, 192000}


class Converter:
    """Base class for audio codecs."""

    codec_lib: str
    container: str
    lossless: bool = False
    default_ffmpeg_arg: str = ""
    # Subclasses set this to False when the muxer doesn't accept -c:v copy.
    # Art is then embedded post-conversion via mutagen instead.
    _ffmpeg_supports_art: bool = True

    def __init__(
        self,
        filename: str,
        ffmpeg_arg: str | None = None,
        sampling_rate: int | None = None,
        bit_depth: int | None = None,
        remove_source: bool = False,
    ):
        """Convert `filename` to this codec, next to the original.

        `ffmpeg_arg` sets a lossy codec's quality (see get_quality_arg);
        `sampling_rate` and `bit_depth` cap a lossless conversion's.
        """
        self.ffmpeg_path = find_ffmpeg()
        if self.ffmpeg_path is None:
            raise Exception(
                "Could not find FFmpeg. Install it, or install streamrip's "
                "ffmpeg extra, to convert audio files.",
            )

        self.filename = filename
        self.final_fn = f"{os.path.splitext(filename)[0]}.{self.container}"
        # Unique per conversion: tracks from different albums often share a
        # file name ("01. Intro"), and concurrent conversions used to
        # overwrite each other's temp file.
        self.tempfile = os.path.join(
            gettempdir(),
            f"__streamrip_{uuid.uuid4().hex}_{os.path.basename(self.final_fn)}",
        )
        self.remove_source = remove_source
        self.sampling_rate = sampling_rate
        self.bit_depth = bit_depth
        self.ffmpeg_arg = self.default_ffmpeg_arg if ffmpeg_arg is None else ffmpeg_arg

    async def convert(self):
        """Run ffmpeg, replacing the source file with the converted one."""
        # Read cover art from the source before FFmpeg runs and before any
        # potential source deletion, so it's available for post-conversion
        # embedding even when remove_source=True.
        cover_data: tuple[bytes, str] | None = None
        if not self._ffmpeg_supports_art:
            cover_data = await asyncio.to_thread(self._read_source_cover)

        command = self._gen_command()
        logger.debug("Converting: %s", command)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await process.communicate()
        if process.returncode != 0 or not os.path.isfile(self.tempfile):
            if os.path.exists(self.tempfile):
                os.remove(self.tempfile)
            raise ConversionError(
                f"ffmpeg failed (exit {process.returncode}): "
                f"{err.decode(errors='replace')[-300:]}"
            )

        if self.remove_source:
            os.remove(self.filename)
        shutil.move(self.tempfile, self.final_fn)
        if cover_data is not None:
            await asyncio.to_thread(self._embed_cover_art, *cover_data)

    def _read_source_cover(self) -> tuple[bytes, str] | None:
        """Read cover art from the source file. Returns (data, mime) or None."""
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3

        src_ext = os.path.splitext(self.filename)[1].lower()
        try:
            if src_ext == ".flac":
                src = FLAC(self.filename)
                if src.pictures:
                    p = src.pictures[0]
                    return p.data, p.mime
            elif src_ext == ".mp3":
                tags = ID3(self.filename)
                apic_list = tags.getall("APIC")
                if apic_list:
                    return apic_list[0].data, apic_list[0].mime
        except Exception as e:
            logger.debug("Could not read cover art from source: %s", e)
        return None

    def _embed_cover_art(self, cover_data: bytes, mime: str) -> None:
        """Embed cover art into the converted OGG/OPUS file via METADATA_BLOCK_PICTURE."""
        from mutagen.flac import Picture

        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = mime
        pic.data = cover_data
        encoded = base64.b64encode(pic.write()).decode("ascii")

        out_ext = os.path.splitext(self.final_fn)[1].lower()
        try:
            if out_ext == ".ogg":
                from mutagen.oggvorbis import OggVorbis

                audio = OggVorbis(self.final_fn)
            elif out_ext == ".opus":
                from mutagen.oggopus import OggOpus

                audio = OggOpus(self.final_fn)
            else:
                return
            audio["metadata_block_picture"] = [encoded]
            audio.save()
            logger.debug("Embedded cover art via mutagen into %s", self.final_fn)
        except Exception as e:
            logger.warning("Could not embed cover art into %s: %s", self.final_fn, e)

    def _gen_command(self) -> list[str]:
        """Build the ffmpeg command line that performs the conversion."""
        # Only errors: they're captured for ConversionError, and anything more
        # ffmpeg prints would garble the progress bars.
        command = [self.ffmpeg_path, "-i", self.filename, "-loglevel", "error"]
        command += ["-c:a", self.get_codec_lib()]
        # The cover art is a video stream, copied where the container takes one.
        command += ["-c:v", "copy"] if self._ffmpeg_supports_art else ["-vn"]
        command += self.ffmpeg_arg.split()

        aformat = []
        if self.lossless and self.sampling_rate is not None:
            rates = (str(r) for r in sorted(SAMPLING_RATES) if r <= self.sampling_rate)
            aformat.append(f"sample_rates={'|'.join(rates)}")
        if self.lossless and self.bit_depth is not None:
            if self.bit_depth not in (16, 24, 32):
                raise ValueError("Bit depth must be 16, 24, or 32")
            formats = ["s16p", "s16"] + (["s32p", "s32"] if self.bit_depth > 16 else [])
            aformat.append(f"sample_fmts={'|'.join(formats)}")
        if aformat:
            command += ["-af", f"aformat={':'.join(aformat)}"]

        return [*command, "-y", self.tempfile]

    def get_codec_lib(self) -> str:
        return self.codec_lib

    @classmethod
    def get_quality_arg(cls, _: int) -> str:
        return cls.default_ffmpeg_arg


class FLAC(Converter):
    """Class for FLAC converter."""

    codec_lib = "flac"
    container = "flac"
    lossless = True


class LAME(Converter):
    """Class for libmp3lame converter.

    Default ffmpeg_arg: `-q:a 0`.

    See available options:
    https://trac.ffmpeg.org/wiki/Encode/MP3
    """

    _bitrate_map: Final[dict[int, str]] = {
        320: "-b:a 320k",
        245: "-q:a 0",
        225: "-q:a 1",
        190: "-q:a 2",
        175: "-q:a 3",
        165: "-q:a 4",
        130: "-q:a 5",
        115: "-q:a 6",
        100: "-q:a 7",
        85: "-q:a 8",
        65: "-q:a 9",
    }

    codec_lib = "libmp3lame"
    container = "mp3"
    default_ffmpeg_arg = "-q:a 0"  # V0

    @classmethod
    def get_quality_arg(cls, rate):
        return cls._bitrate_map.get(rate, f"-b:a {rate}k")


class ALAC(Converter):
    """Class for ALAC converter."""

    codec_lib = "alac"
    container = "m4a"
    lossless = True


class Vorbis(Converter):
    """Class for libvorbis converter.

    Default ffmpeg_arg: `-q:a 6`.

    See available options:
    https://trac.ffmpeg.org/wiki/TheoraVorbisEncodingGuide
    """

    codec_lib = "libvorbis"
    container = "ogg"
    # The OGG muxer doesn't support -c:v copy; art is embedded via mutagen instead.
    _ffmpeg_supports_art = False
    default_ffmpeg_arg = "-q:a 6"  # 160, aka the "high" quality profile from Spotify

    @classmethod
    def get_quality_arg(cls, rate: int) -> str:
        arg = "-qscale:a %d"
        if rate <= 128:
            return arg % (rate / 16 - 4)
        if rate <= 256:
            return arg % (rate / 32)

        return arg % (rate / 64 + 4)


class OPUS(Converter):
    """Class for libopus.

    Default ffmpeg_arg: `-b:a 128 -vbr on`.

    See more:
    http://ffmpeg.org/ffmpeg-codecs.html#libopus-1
    """

    codec_lib = "libopus"
    container = "opus"
    # The Opus muxer doesn't support -c:v copy; art is embedded via mutagen instead.
    _ffmpeg_supports_art = False
    default_ffmpeg_arg = "-b:a 128k"  # Transparent

    @classmethod
    def get_quality_arg(cls, rate: int) -> str:
        return f"-b:a {rate}k"


class AAC(Converter):
    """Class for AAC converter.

    Uses libfdk_aac when ffmpeg was built with it, and ffmpeg's native aac
    encoder otherwise; most ffmpeg builds lack libfdk_aac, which made every
    AAC conversion fail (upstream #1010).

    Default ffmpeg_arg: `-b:a 256k`.

    See available options:
    https://trac.ffmpeg.org/wiki/Encode/AAC
    """

    codec_lib = "libfdk_aac"
    container = "m4a"
    default_ffmpeg_arg = "-b:a 256k"

    def get_codec_lib(self) -> str:
        return "libfdk_aac" if _ffmpeg_has_encoder("libfdk_aac") else "aac"

    @classmethod
    def get_quality_arg(cls, rate: int) -> str:
        return f"-b:a {rate}k"


class AIFF(Converter):
    """Class for AIFF converter (uncompressed PCM, upstream PR #1006)."""

    codec_lib = "pcm_s24be"
    container = "aiff"
    lossless = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Keep 16-bit sources 16-bit instead of padding them to 24.
        if self.bit_depth == 16:
            self.codec_lib = "pcm_s16be"


@functools.cache
def _ffmpeg_has_encoder(name: str) -> bool:
    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path is None:
        return False
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return name in result.stdout


def get(codec: str) -> type[Converter]:
    converter_classes = {
        "FLAC": FLAC,
        "ALAC": ALAC,
        "MP3": LAME,
        "OPUS": OPUS,
        "OGG": Vorbis,
        "VORBIS": Vorbis,
        "AAC": AAC,
        "M4A": AAC,
        "AIFF": AIFF,
    }
    return converter_classes[codec.upper()]
