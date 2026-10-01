import json
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import mutagen
import pytest
from util import arun

import streamrip.db as db
from streamrip.client.downloadable import Downloadable
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError
from streamrip.media.track import PendingSingle, PendingTrack, Track, album_folder
from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
)


@pytest.mark.skipif(
    not (os.environ.get("QOBUZ_USER_ID") and os.environ.get("QOBUZ_AUTH_TOKEN")),
    reason="Qobuz user ID and auth token are required.",
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


@pytest.mark.asyncio
async def test_single_without_download_info_is_kept_for_repair(tmp_path, monkeypatch):
    # This used to escape resolve() and never reach the failed database.
    monkeypatch.setattr(
        "streamrip.media.track.download_artwork", AsyncMock(return_value=(None, None))
    )
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path)
    client = MagicMock()
    client.source = "deezer"
    client.get_metadata = AsyncMock(
        return_value={
            "id": 7,
            "title": "Song",
            "artist": {"name": "Artist"},
            "track_position": 1,
            "disk_number": 1,
            "album": {
                "id": 70,
                "title": "Album",
                **{f"cover_{s}": "u" for s in ("xl", "big", "medium", "small")},
            },
        }
    )
    client.get_downloadable = AsyncMock(side_effect=NonStreamableError("geoblocked"))
    database = MagicMock()
    database.downloaded.return_value = False

    assert await PendingSingle("7", client, config, database).resolve() is None
    database.set_failed.assert_called_once_with("deezer", "track", "7")


@pytest.mark.parametrize("restrict", [False, True])
def test_album_folder_follows_restrict_characters(restrict):
    # Singles built their album folder without restrict_characters, so with
    # it on they landed beside their album instead of in it.
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.filepaths.folder_format = "{albumartist} - {title}"
    config.session.filepaths.restrict_characters = restrict
    album = AlbumMetadata(
        AlbumInfo("1", 2, "FLAC"), "Homogénic", "Björk", "1997", [], Covers(), 10
    )

    folder = album_folder(config, "qobuz", album)

    assert folder == (
        "/music/Bjrk - Homognic" if restrict else "/music/Björk - Homogénic"
    )


@pytest.fixture(params=["single", "album"])
def pending_disc_track(request, tmp_path, monkeypatch):
    with open("tests/qobuz_track_resp.json") as f:
        resp = json.load(f)
    resp["album"]["media_count"] = 2
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path / "downloads")
    config.session.downloads.disc_subdirectories = True
    config.session.filepaths.add_singles_to_folder = True
    config.session.filepaths.folder_format = "{title}"
    config.session.filepaths.track_format = "{title}"
    config.session.cli.progress_bars = False
    client = MagicMock()
    client.source = "qobuz"
    client.get_metadata = AsyncMock(return_value=resp)
    client.get_downloadable = AsyncMock(return_value=FakeDownloadable("flac"))
    database = MagicMock()
    database.downloaded.return_value = False
    artwork = AsyncMock(return_value=((None, None), None))
    monkeypatch.setattr("streamrip.media.track.download_artwork", artwork)
    track_id = str(resp["id"])
    if request.param == "single":
        pending = PendingSingle(track_id, client, config, database)
    else:
        album = AlbumMetadata.from_qobuz(resp["album"])
        pending = PendingTrack(
            track_id,
            album,
            client,
            config,
            album_folder(config, client.source, album),
            database,
            None,
        )
    return pending, resp, artwork


@pytest.mark.parametrize(
    "discnumber",
    ["1/../../../escaped", r"1\..\..\..\escaped", "2", "", None, True, 0, -1, 1.5],
)
@pytest.mark.asyncio
async def test_invalid_disc_number_is_rejected_before_io(
    pending_disc_track, tmp_path, discnumber
):
    pending, resp, artwork = pending_disc_track
    resp["media_number"] = discnumber

    assert await pending.resolve() is None

    pending.db.set_failed.assert_called_once_with("qobuz", "track", pending.id)
    pending.client.get_downloadable.assert_not_awaited()
    artwork.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("discnumber", [1, 2, None])
@pytest.mark.asyncio
async def test_valid_disc_number_download_stays_in_album(
    pending_disc_track, discnumber
):
    pending, resp, _ = pending_disc_track
    if discnumber is None:
        del resp["media_number"]  # A missing Qobuz disc number defaults to 1.
    else:
        resp["media_number"] = discnumber

    track = await pending.resolve()
    assert track is not None
    expected = Path(album_folder(pending.config, "qobuz", track.meta.album)) / (
        f"Disc {discnumber or 1}"
    )
    assert Path(track.folder) == expected
    await track.preprocess()
    await track.download()
    assert Path(track.download_path).parent == expected
    assert Path(track.download_path).read_bytes() == Path(FIXTURES["flac"]).read_bytes()
    pending.db.set_failed.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preprocess", "postprocess"])
@pytest.mark.parametrize("is_single", [False, True])
async def test_processing_failures_are_counted_and_stored_for_repair(
    tmp_path, monkeypatch, phase, is_single
):
    """Filesystem and tag failures contribute to totals for every track path."""
    track = _make_track(str(tmp_path), "flac")
    track.is_single = is_single
    track.db = db.Database(db.Dummy(), db.Failed(str(tmp_path / "failed.db")))
    monkeypatch.setattr(
        Track, phase, AsyncMock(side_effect=OSError("processing failed"))
    )
    with pytest.raises(OSError, match="processing failed"):
        await track.rip()
    assert track.db.failed_now == 1
    assert track.db.downloaded_now == 0
    assert track.db.failed.all() == [("test", "track", "123")]


@pytest.mark.asyncio
async def test_exhausted_track_download_is_counted_only_once(tmp_path, monkeypatch):
    """The outer processing handler preserves download failure accounting."""
    from streamrip.exceptions import TrackDownloadFailedError

    track = _make_track(str(tmp_path), "flac")
    monkeypatch.setattr(
        FakeDownloadable, "_download", AsyncMock(side_effect=OSError("download failed"))
    )
    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", AsyncMock())
    with pytest.raises(TrackDownloadFailedError):
        await track.rip()
    assert track.db.failed_now == 1
    assert track.db.downloaded_now == 0


@pytest.mark.asyncio
async def test_partial_file_cleanup_failure_is_counted_once(tmp_path, monkeypatch):
    """A cleanup error is counted by the outer handler, not twice."""
    track = _make_track(str(tmp_path), "flac")
    # A real failed table: the Dummy one never stores anything.
    track.db = db.Database(db.Dummy(), db.Failed(str(tmp_path / "failed.db")))
    monkeypatch.setattr(
        FakeDownloadable, "_download", AsyncMock(side_effect=OSError("download failed"))
    )
    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("streamrip.media.track.os.path.isfile", lambda _: True)
    monkeypatch.setattr(
        "streamrip.media.track.os.remove",
        MagicMock(side_effect=OSError("cleanup failed")),
    )

    with pytest.raises(OSError, match="cleanup failed"):
        await track.rip()

    assert track.db.failed_now == 1
    assert track.db.failed.all() == [("test", "track", "123")]
