import os
import shutil

import mutagen
import pytest
from util import arun

import streamrip.db as db
from streamrip.client.downloadable import Downloadable
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.media.track import PendingSingle, Track
from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
)


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_pending_resolve(qobuz_client: QobuzClient):
    qobuz_client.config.session.downloads.folder = "./tests"
    p = PendingSingle(
        "19512574",
        qobuz_client,
        qobuz_client.config,
        db.Database(db.Dummy(), db.Dummy()),
    )
    t = arun(p.resolve())
    dir = "tests/tests/Fleetwood Mac - Rumours (1977) [FLAC] [24B-96kHz]"
    assert os.path.isdir(dir)
    assert os.path.isfile(os.path.join(dir, "cover.jpg"))
    assert os.path.isfile(t.cover_path)
    assert isinstance(t, Track)
    assert isinstance(t.downloadable, Downloadable)
    assert t.cover_path is not None
    shutil.rmtree(dir)


FIXTURES = {
    "flac": "tests/silence.flac",
    "m4a": "tests/silence.m4a",
    "alac": "tests/silence_alac.m4a",
}


class FakeDownloadable(Downloadable):
    """Copies a real audio fixture into place, so Track can tag it afterwards."""

    def __init__(self, extension: str):
        self.extension = extension
        self.source = "test"

    async def _download(self, path, callback):
        shutil.copy(FIXTURES[self.extension], path)
        callback(os.path.getsize(path))

    async def size(self):
        return os.path.getsize(FIXTURES[self.extension])


def _place_copy(path, fixture: str, album: str = "Test Album"):
    """An earlier download of a track: real audio, tagged like streamrip does."""
    shutil.copy(FIXTURES[fixture], path)
    audio = mutagen.File(path, easy=True)
    audio["title"] = "Song"
    audio["album"] = album
    audio.save()


def _make_track(folder: str, extension: str) -> Track:
    config = Config.defaults()
    config.session.downloads.folder = folder
    config.session.cli.progress_bars = False
    config.session.filepaths.track_format = "{title}"

    album = AlbumMetadata(
        AlbumInfo("1", 2, "flac"),
        "Test Album",
        "Test Artist",
        "2020",
        [],
        Covers(),
        1,
    )
    meta = TrackMetadata(
        info=TrackInfo(id="123"),
        title="Song",
        album=album,
        artist="Test Artist",
        tracknumber=1,
        discnumber=1,
        composer=None,
    )
    return Track(
        meta,
        FakeDownloadable(extension),
        config,
        folder,
        None,
        db.Database(db.Dummy(), db.Dummy()),
    )


def test_lossy_download_skipped_when_lossless_copy_exists(tmp_path):
    _place_copy(tmp_path / "Song.flac", "flac")

    arun(_make_track(str(tmp_path), "m4a").rip())

    assert not (tmp_path / "Song.m4a").exists()
    assert (tmp_path / "Song.flac").exists()


def test_lossless_copy_of_another_release_does_not_skip(tmp_path):
    # Same filename stem, different album: not the same track.
    _place_copy(tmp_path / "Song.flac", "flac", album="Some Single")

    arun(_make_track(str(tmp_path), "m4a").rip())

    assert (tmp_path / "Song.m4a").exists()


def test_lossy_download_proceeds_without_a_lossless_copy(tmp_path):
    arun(_make_track(str(tmp_path), "m4a").rip())

    assert (tmp_path / "Song.m4a").exists()


def test_lossy_copy_removed_once_lossless_copy_lands(tmp_path):
    _place_copy(tmp_path / "Song.m4a", "m4a")

    arun(_make_track(str(tmp_path), "flac").rip())

    assert (tmp_path / "Song.flac").exists()
    assert not (tmp_path / "Song.m4a").exists()


def test_lossy_copy_of_another_release_is_kept(tmp_path):
    _place_copy(tmp_path / "Song.m4a", "m4a", album="Some Single")

    arun(_make_track(str(tmp_path), "flac").rip())

    assert (tmp_path / "Song.m4a").exists()


def test_alac_copy_is_never_treated_as_lossy(tmp_path):
    # ALAC is lossless but shares the .m4a extension with AAC.
    _place_copy(tmp_path / "Song.m4a", "alac")

    arun(_make_track(str(tmp_path), "flac").rip())

    assert (tmp_path / "Song.m4a").exists()


class FlacToFlacConverter:
    """Stands in for a lossless conversion that keeps the format (downsampling)."""

    lossless = True

    def __init__(self, filename, **_):
        self.final_fn = filename

    async def convert(self):
        pass


def test_lossy_copy_removed_after_lossless_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "streamrip.media.track.converter.get", lambda _: FlacToFlacConverter
    )
    _place_copy(tmp_path / "Song.m4a", "m4a")
    t = _make_track(str(tmp_path), "flac")
    t.config.session.conversion.enabled = True

    arun(t.rip())

    assert (tmp_path / "Song.flac").exists()
    assert not (tmp_path / "Song.m4a").exists()
