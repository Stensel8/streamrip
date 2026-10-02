import shutil
from types import SimpleNamespace

import pytest
from mutagen.flac import FLAC

from streamrip.utils import integrity
from streamrip.utils.integrity import check_integrity


def _truncated_flac(path, seconds=60):
    """A FLAC whose header announces far more audio than the file holds."""
    shutil.copy("tests/silence.flac", path)
    audio = FLAC(path)
    audio.info.total_samples = audio.info.sample_rate * seconds
    audio.save()
    return str(path)


def _fake_audio(monkeypatch, path, size, **info):
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    monkeypatch.setattr(
        integrity.mutagen,
        "File",
        lambda _: SimpleNamespace(info=SimpleNamespace(**info)),
    )
    return str(path)


def test_truncated_flac_is_reported(tmp_path):
    problem = check_integrity(_truncated_flac(tmp_path / "t.flac"))
    assert problem is not None
    assert "truncated" in problem


def test_short_fixture_passes(tmp_path):
    # Under five seconds, there is nothing reliable to judge.
    assert check_integrity("tests/silence.flac") is None


def test_empty_file_is_reported(tmp_path):
    path = tmp_path / "t.flac"
    path.write_bytes(b"")
    assert check_integrity(str(path)) == "the file is empty"


def test_unparseable_file_is_reported(tmp_path):
    path = tmp_path / "t.flac"
    path.write_bytes(b"fLaC" + b"\xff" * 64)
    assert "cannot be parsed" in check_integrity(str(path))


def test_unknown_format_passes(tmp_path):
    path = tmp_path / "t.bin"
    path.write_bytes(b"not audio at all" * 100)
    assert check_integrity(str(path)) is None


@pytest.mark.parametrize(
    ("kbps", "info", "ok"),
    [
        # MP3 128 is far above the lossy floor; a collapse is not.
        (128, {"codec": "mp3"}, True),
        (20, {"codec": "mp3"}, False),
        # Lossy floors must not apply to lossless files, and the other way.
        (80, {"codec": "mp4a.40.2"}, True),
        (80, {"codec": "alac", "bits_per_sample": 16, "sample_rate": 44100}, False),
        (150, {"codec": "flac", "bits_per_sample": 16, "sample_rate": 44100}, True),
        (150, {"codec": "flac", "bits_per_sample": 24, "sample_rate": 96000}, False),
    ],
)
def test_floor_follows_the_file_format(monkeypatch, tmp_path, kbps, info, ok):
    duration = 10
    size = kbps * 1000 * duration // 8
    path = _fake_audio(monkeypatch, tmp_path / "t", size, length=duration, **info)
    assert (check_integrity(path) is None) is ok
