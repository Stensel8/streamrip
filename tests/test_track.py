import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import mutagen
import pytest

import streamrip.db as db
from streamrip.client.downloadable import Downloadable
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError
from streamrip.media.track import (
    PendingSingle,
    PendingTrack,
    Track,
    album_folder,
    fetch_track_meta,
)
from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
)

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
        callback(path.stat().st_size)

    async def size(self):
        return Path(FIXTURES[self.extension]).stat().st_size


def _place_copy(path, fixture: str, album: str = "Test Album"):
    """An earlier download of a track: real audio, tagged like streamrip does."""
    shutil.copy(FIXTURES[fixture], path)
    audio = mutagen.File(path, easy=True)
    audio["title"] = "Song"
    audio["album"] = album
    audio.save()


def _make_track(folder: Path, extension: str) -> Track:
    config = Config.defaults()
    config.session.downloads.folder = str(folder)
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


async def test_lossy_download_skipped_when_lossless_copy_exists(tmp_path):
    _place_copy(tmp_path / "Song.flac", "flac")

    await _make_track(tmp_path, "m4a").rip()

    assert not (tmp_path / "Song.m4a").exists()
    assert (tmp_path / "Song.flac").exists()


@pytest.mark.parametrize(("existing", "downloaded"), [("flac", "m4a"), ("m4a", "flac")])
async def test_a_copy_of_another_release_is_neither_skipped_for_nor_removed(
    tmp_path, existing, downloaded
):
    # Same filename stem, different album: not the same track.
    _place_copy(tmp_path / f"Song.{existing}", existing, album="Some Single")

    await _make_track(tmp_path, downloaded).rip()

    assert (tmp_path / f"Song.{downloaded}").exists()
    assert (tmp_path / f"Song.{existing}").exists()


async def test_lossy_download_proceeds_without_a_lossless_copy(tmp_path):
    await _make_track(tmp_path, "m4a").rip()

    assert (tmp_path / "Song.m4a").exists()


async def test_lossy_copy_removed_once_lossless_copy_lands(tmp_path):
    _place_copy(tmp_path / "Song.m4a", "m4a")

    await _make_track(tmp_path, "flac").rip()

    assert (tmp_path / "Song.flac").exists()
    assert not (tmp_path / "Song.m4a").exists()


async def test_alac_copy_is_never_treated_as_lossy(tmp_path):
    # ALAC is lossless but shares the .m4a extension with AAC.
    _place_copy(tmp_path / "Song.m4a", "alac")

    await _make_track(tmp_path, "flac").rip()

    assert (tmp_path / "Song.m4a").exists()


class FlacToFlacConverter:
    """Stands in for a lossless conversion that keeps the format (downsampling)."""

    lossless = True

    def __init__(self, filename, **_):
        self.final_fn = filename

    async def convert(self):
        pass


async def test_lossy_copy_removed_after_lossless_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "streamrip.media.track.converter.get", lambda _: FlacToFlacConverter
    )
    _place_copy(tmp_path / "Song.m4a", "m4a")
    t = _make_track(tmp_path, "flac")
    t.config.session.conversion.enabled = True

    await t.rip()

    assert (tmp_path / "Song.flac").exists()
    assert not (tmp_path / "Song.m4a").exists()


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
    client.before_track = AsyncMock()
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

    assert folder == Path(
        "/music/Bjrk - Homognic" if restrict else "/music/Björk - Homogénic"
    )


@pytest.fixture(params=["single", "album"])
def pending_disc_track(request, tmp_path, monkeypatch):
    resp = json.loads(Path("tests/qobuz_track_resp.json").read_text())
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
    client.before_track = AsyncMock()
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
    expected = album_folder(pending.config, "qobuz", track.meta.album) / (
        f"Disc {discnumber or 1}"
    )
    assert track.folder == expected
    await track.preprocess()
    await track.download()
    assert track.download_path.parent == expected
    assert track.download_path.read_bytes() == Path(FIXTURES["flac"]).read_bytes()
    pending.db.set_failed.assert_not_called()


@pytest.mark.parametrize("phase", ["preprocess", "postprocess"])
@pytest.mark.parametrize("is_single", [False, True])
async def test_processing_failures_are_counted_and_stored_for_repair(
    tmp_path, monkeypatch, phase, is_single
):
    """Filesystem and tag failures contribute to totals for every track path."""
    track = _make_track(tmp_path, "flac")
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


async def test_exhausted_track_download_is_counted_only_once(tmp_path, monkeypatch):
    """The outer processing handler preserves download failure accounting."""
    from streamrip.exceptions import TrackDownloadFailedError

    track = _make_track(tmp_path, "flac")
    monkeypatch.setattr(
        FakeDownloadable, "_download", AsyncMock(side_effect=OSError("download failed"))
    )
    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", AsyncMock())
    with pytest.raises(TrackDownloadFailedError):
        await track.rip()
    assert track.db.failed_now == 1
    assert track.db.downloaded_now == 0


async def test_partial_file_cleanup_failure_is_counted_once(tmp_path, monkeypatch):
    """A cleanup error is counted by the outer handler, not twice."""
    track = _make_track(tmp_path, "flac")
    # A real failed table: the Dummy one never stores anything.
    track.db = db.Database(db.Dummy(), db.Failed(str(tmp_path / "failed.db")))
    real_unlink = Path.unlink

    async def download(self, path, callback):
        path.write_bytes(b"partial")  # what the cleanup then fails to remove
        raise OSError("download failed")

    def unlink(self, *args, **kwargs):
        if self.suffix == ".flac":
            raise OSError("cleanup failed")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(FakeDownloadable, "_download", download)
    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(OSError, match="cleanup failed"):
        await track.rip()

    assert track.db.failed_now == 1
    assert track.db.failed.all() == [("test", "track", "123")]


async def test_a_downloaded_soundcloud_track_is_skipped_whatever_id_it_is_asked_for_by(
    tmp_path,
):
    # SoundCloud ids carry the stream to fetch ("123|https://..."); the track
    # is recorded under the number. A live run downloaded it again every time.
    database = db.Database(db.Downloads(str(tmp_path / "downloads.db")), db.Dummy())
    database.set_downloaded("soundcloud", "671678984")
    client = MagicMock()
    client.source = "soundcloud"
    client.get_metadata = AsyncMock()

    meta = await fetch_track_meta(client, database, "671678984|https://x/stream")

    assert meta is None
    assert database.skipped_now == 1
    client.get_metadata.assert_not_called()


async def test_a_track_that_will_download_is_paced_like_a_person(tmp_path):
    database = db.Database(db.Downloads(str(tmp_path / "downloads.db")), db.Dummy())
    client = MagicMock()
    client.source = "test"
    client.get_metadata = AsyncMock(side_effect=RuntimeError("service down"))
    client.before_track = AsyncMock()

    await fetch_track_meta(client, database, "123")

    client.before_track.assert_awaited_once()

    # A track already in the database makes no request, so it waits for none.
    database.set_downloaded("test", "456")
    client.before_track.reset_mock()
    await fetch_track_meta(client, database, "456")

    client.before_track.assert_not_awaited()
    assert database.skipped_now == 1
