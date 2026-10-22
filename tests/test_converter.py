import shutil
from pathlib import Path

import mutagen
import pytest

from streamrip import converter
from streamrip.config import Config
from streamrip.exceptions import ConversionError
from streamrip.filepath_utils import fit_filename
from streamrip.media.track import Track
from streamrip.utils.ffmpeg_utils import find_ffmpeg


class DummyConverter:
    lossless = False
    instances = []

    @classmethod
    def get_quality_arg(cls, rate):
        return f"-b:a {rate}k"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.final_fn = Path("/tmp/output.opus")
        self.instances.append(self)

    async def convert(self):
        pass


class DummyLosslessConverter(DummyConverter):
    lossless = True
    instances = []

    @classmethod
    def get_quality_arg(cls, rate):
        raise AssertionError("lossless converters should not use lossy_bitrate")


async def test_lossy_bitrate_config_is_passed_to_converter(monkeypatch):
    monkeypatch.setattr("streamrip.media.track.converter.get", lambda _: DummyConverter)
    DummyConverter.instances = []

    config = Config.defaults()
    config.session.conversion.codec = "OPUS"
    config.session.conversion.lossy_bitrate = 160
    track = Track(
        None, None, config, Path(), None, None, download_path=Path("/tmp/source.flac")
    )

    await track._convert()

    engine = DummyConverter.instances[0]
    assert engine.kwargs["ffmpeg_arg"] == "-b:a 160k"
    assert track.download_path == Path("/tmp/output.opus")


async def test_lossy_bitrate_config_is_not_passed_to_lossless_converter(monkeypatch):
    monkeypatch.setattr(
        "streamrip.media.track.converter.get", lambda _: DummyLosslessConverter
    )
    DummyLosslessConverter.instances = []

    config = Config.defaults()
    config.session.conversion.codec = "ALAC"
    config.session.conversion.lossy_bitrate = 160
    track = Track(
        None, None, config, Path(), None, None, download_path=Path("/tmp/source.flac")
    )

    await track._convert()

    engine = DummyLosslessConverter.instances[0]
    assert engine.kwargs["ffmpeg_arg"] is None


def test_lossy_codec_quality_args_use_configured_bitrate():
    assert converter.OPUS.get_quality_arg(160) == "-b:a 160k"
    assert converter.AAC.get_quality_arg(192) == "-b:a 192k"
    assert converter.LAME.get_quality_arg(160) == "-b:a 160k"
    assert converter.Vorbis.get_quality_arg(160) == "-qscale:a 5"


needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="needs ffmpeg")


@needs_ffmpeg
@pytest.mark.parametrize(
    "codec, extension",
    [
        ("FLAC", "flac"),
        ("ALAC", "m4a"),
        ("AIFF", "aiff"),
        ("MP3", "mp3"),
        ("AAC", "m4a"),
        ("OPUS", "opus"),
        ("OGG", "ogg"),
    ],
)
async def test_conversion(tmp_path, codec, extension):
    source = tmp_path / "track.wav"
    shutil.copy("tests/silence.flac", tmp_path / "track.flac")
    (tmp_path / "track.flac").rename(source)  # any input ffmpeg can read
    engine = converter.get(codec)(
        source, sampling_rate=48000, bit_depth=16, remove_source=True
    )

    await engine.convert()

    assert engine.final_fn == tmp_path / f"track.{extension}"
    assert mutagen.File(engine.final_fn) is not None
    assert not source.exists()


@needs_ffmpeg
async def test_failed_conversion_says_why_and_cleans_up(tmp_path):
    source = tmp_path / "track.flac"
    source.write_bytes(b"not audio")
    engine = converter.LAME(source, remove_source=True)

    with pytest.raises(ConversionError, match="ffmpeg failed") as err:
        await engine.convert()

    # ffmpeg's own words, not just that it failed (the wording varies).
    assert str(err.value).split("): ", 1)[1].strip()
    assert source.exists()
    assert not engine.tempfile.exists()


@needs_ffmpeg
async def test_a_name_as_long_as_a_file_name_may_be_converts(tmp_path):
    """The temp file ffmpeg writes doesn't add to the name: it would not fit."""
    source = tmp_path / fit_filename("語" * 100, "flac")
    shutil.copy("tests/silence.flac", source)
    engine = converter.LAME(source, remove_source=True)

    await engine.convert()

    assert mutagen.File(engine.final_fn) is not None
