import os
import shutil

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


class FakeDownloadable(Downloadable):
    """A Downloadable that just writes some bytes, for exercising Track's
    file-placement logic without hitting the network.
    """

    def __init__(self, extension: str):
        self.extension = extension
        self.source = "test"

    async def _download(self, path, callback):
        data = b"fake audio bytes"
        with open(path, "wb") as f:
            f.write(data)
        callback(len(data))

    async def size(self):
        return 17


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
        info=TrackInfo(id="123", quality=2),
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
    (tmp_path / "Song.flac").write_bytes(b"pretend flac bytes")

    t = _make_track(str(tmp_path), "m4a")
    arun(t.preprocess())
    assert t._skip_lossy_duplicate is True

    arun(t.download())
    assert not (tmp_path / "Song.m4a").exists()


def test_lossy_download_proceeds_without_a_lossless_copy(tmp_path):
    t = _make_track(str(tmp_path), "m4a")
    arun(t.preprocess())
    assert t._skip_lossy_duplicate is False

    arun(t.download())
    assert (tmp_path / "Song.m4a").exists()


def test_stale_lossy_sibling_removed_once_lossless_copy_lands(tmp_path):
    (tmp_path / "Song.m4a").write_bytes(b"stale aac copy")

    t = _make_track(str(tmp_path), "flac")
    arun(t.preprocess())
    assert t._skip_lossy_duplicate is False

    arun(t.download())
    assert (tmp_path / "Song.flac").exists()
    assert (tmp_path / "Song.m4a").exists()  # not cleaned up until postprocess

    t._remove_lossy_duplicates()
    assert not (tmp_path / "Song.m4a").exists()
