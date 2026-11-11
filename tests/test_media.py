"""Tracks, albums, artists, labels and playlists: resolving and downloading them."""

import asyncio
import json
import shutil
from contextlib import asynccontextmanager, contextmanager
from itertools import starmap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import mutagen
import pytest
import requests

from streamrip import db
from streamrip.client.audio_match import MatchTrack
from streamrip.client.downloadable import Downloadable
from streamrip.config import ArtistFilterConfig, Config
from streamrip.exceptions import NonStreamableError, TrackDownloadFailedError
from streamrip.media import album as album_module
from streamrip.media import artwork
from streamrip.media.album import RESOLVE_CONCURRENCY, Album, PendingAlbum
from streamrip.media.artist import Artist, Label
from streamrip.media.csv_playlist import PendingCsvPlaylist, read_tracks
from streamrip.media.media import filter_prefer_explicit
from streamrip.media.playlist import (
    LASTFM_MAX_TRACKS,
    PendingLastfmPlaylist,
    PendingPlaylist,
    PendingPlaylistTrack,
    _playlist_folder,
    best_result,
    queries,
)
from streamrip.media.track import (
    PendingSingle,
    PendingTrack,
    Track,
    album_folder,
    fetch_downloadable,
    fetch_track_meta,
)
from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    PlaylistMetadata,
    Summary,
    TrackInfo,
    TrackMetadata,
)

# --- tracks -------------------------------------------------------------------

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


class FlacToFlacConverter:
    """Stands in for a lossless conversion that keeps the format (downsampling)."""

    lossless = True

    def __init__(self, filename, **_):
        self.final_fn = filename

    async def convert(self):
        pass


def _make_track(folder: Path, extension: str) -> Track:
    config = Config.defaults()
    config.session.downloads.folder = str(folder)
    config.session.cli.progress_bars = False
    config.session.filepaths.track_format = "{title}"
    album = AlbumMetadata(
        AlbumInfo("1", 2, "flac"), "Test Album", "Test Artist", "2020", [], Covers(), 1
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
    database = db.Database(db.Dummy(), db.Dummy())
    return Track(meta, FakeDownloadable(extension), config, folder, None, database)


def _failed_db(tmp_path):
    """A real failed table: the Dummy one never stores anything."""
    return db.Database(db.Dummy(), db.Failed(str(tmp_path / "failed.db")))


@pytest.mark.parametrize(
    ("existing", "album", "downloaded", "convert", "left"),
    [
        (None, None, "m4a", False, ["Song.m4a"]),
        ("flac", "Test Album", "m4a", False, ["Song.flac"]),  # lossy one not fetched
        ("m4a", "Test Album", "flac", False, ["Song.flac"]),  # lossy one removed
        ("m4a", "Test Album", "flac", True, ["Song.flac"]),  # also after converting
        # ALAC is lossless but shares the .m4a extension with AAC.
        ("alac", "Test Album", "flac", False, ["Song.flac", "Song.m4a"]),
        # Same filename stem, another album: not the same track.
        ("flac", "Some Single", "m4a", False, ["Song.flac", "Song.m4a"]),
        ("m4a", "Some Single", "flac", False, ["Song.flac", "Song.m4a"]),
    ],
)
async def test_of_two_copies_of_a_track_only_the_lossless_one_is_kept(
    tmp_path, monkeypatch, existing, album, downloaded, convert, left
):
    if existing:  # an earlier download: real audio, tagged as streamrip does
        path = tmp_path / ("Song.flac" if existing == "flac" else "Song.m4a")
        shutil.copy(FIXTURES[existing], path)
        audio = mutagen.File(path, easy=True)
        audio["title"] = "Song"
        audio["album"] = album
        audio.save()
    monkeypatch.setattr(
        "streamrip.media.track.converter.get", lambda _: FlacToFlacConverter
    )
    track = _make_track(tmp_path, downloaded)
    track.config.session.conversion.enabled = convert

    await track.rip()

    assert sorted(p.name for p in tmp_path.iterdir()) == left


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
    """A track of a two-disc Qobuz album: as a single, or as one of its album's."""
    resp = json.loads(Path("tests/qobuz_track_resp.json").read_text(encoding="utf-8"))
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
        folder = album_folder(config, client.source, album)
        pending = PendingTrack(track_id, album, client, config, folder, database, None)
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


@pytest.mark.parametrize("pending_disc_track", ["single"], indirect=True)
async def test_create_album_folder_off_skips_the_disc_subfolder_too(
    pending_disc_track,
):
    # A single with create_album_folder off goes straight in the downloads
    # folder (only a single reads it for its own folder); a multi-disc
    # "Disc N" subfolder there would contradict that.
    pending, _, artwork = pending_disc_track
    pending.config.session.downloads.create_album_folder = False

    track = await pending.resolve()

    assert track is not None
    assert track.folder == Path(pending.config.session.downloads.folder)
    assert artwork.call_args.kwargs["shared_folder"] is True


@pytest.mark.parametrize("pending_disc_track", ["single"], indirect=True)
async def test_a_single_without_download_info_is_kept_for_repair(
    pending_disc_track,
):
    # This used to escape resolve() and never reach the failed database.
    pending, _, _ = pending_disc_track
    pending.client.get_downloadable.side_effect = NonStreamableError("geoblocked")

    assert await pending.resolve() is None
    pending.db.set_failed.assert_called_once_with("qobuz", "track", pending.id)


@pytest.mark.parametrize("phase", ["preprocess", "postprocess"])
@pytest.mark.parametrize("is_single", [False, True])
async def test_processing_failures_are_counted_and_stored_for_repair(
    tmp_path, monkeypatch, phase, is_single
):
    track = _make_track(tmp_path, "flac")
    track.is_single = is_single
    track.db = _failed_db(tmp_path)
    monkeypatch.setattr(
        Track, phase, AsyncMock(side_effect=OSError("processing failed"))
    )
    with pytest.raises(OSError, match="processing failed"):
        await track.rip()
    assert (track.db.failed_now, track.db.downloaded_now) == (1, 0)
    assert track.db.failed.all() == [("test", "track", "123")]


@pytest.mark.parametrize(
    ("cleanup_fails", "raised"),
    [(False, TrackDownloadFailedError), (True, OSError)],
    ids=["download gives up", "its cleanup fails too"],
)
async def test_a_failed_download_is_counted_once(
    tmp_path, monkeypatch, cleanup_fails, raised
):
    # By rip()'s handler, not by download()'s as well.
    track = _make_track(tmp_path, "flac")
    track.db = _failed_db(tmp_path)
    real_unlink = Path.unlink

    async def download(self, path, callback):
        path.write_bytes(b"partial")  # what the cleanup has to remove
        raise OSError("download failed")

    def unlink(self, *args, **kwargs):
        if cleanup_fails and self.suffix == ".flac":
            raise OSError("cleanup failed")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(FakeDownloadable, "_download", download)
    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(raised):
        await track.rip()

    assert (track.db.failed_now, track.db.downloaded_now) == (1, 0)
    assert track.db.failed.all() == [("test", "track", "123")]


async def test_a_track_waiting_to_retry_leaves_its_slot_to_another(
    tmp_path, monkeypatch
):
    second_done = asyncio.Event()

    class FailsOnce(FakeDownloadable):
        failed = False

        async def _download(self, path, callback):
            if not self.failed:
                self.failed = True
                raise OSError("connection dropped")
            await super()._download(path, callback)

    class Second(FakeDownloadable):
        async def _download(self, path, callback):
            await super()._download(path, callback)
            second_done.set()

    async def backoff(_delay):
        # Waits until the other track got the one slot and finished.
        await asyncio.wait_for(second_done.wait(), 2)

    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", backoff)
    tracks = []
    for name, downloadable in (("a", FailsOnce("flac")), ("b", Second("flac"))):
        track = _make_track(tmp_path / name, "flac")
        track.config.session.downloads.max_connections = 1
        track.downloadable = downloadable
        await track.preprocess()
        tracks.append(track)

    await asyncio.gather(*(t.download() for t in tracks))

    assert all(t.download_path.is_file() for t in tracks)


async def test_a_download_that_fails_leaves_a_copy_already_there_alone(
    tmp_path, monkeypatch
):
    # With --no-db a finished copy can be at the path; a retry used to resume
    # from it, and the last failure deleted it.
    track = _make_track(tmp_path, "flac")
    await track.preprocess()
    track.download_path.write_bytes(b"the good copy")
    monkeypatch.setattr(
        FakeDownloadable, "_download", AsyncMock(side_effect=OSError("network down"))
    )
    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", AsyncMock())

    with pytest.raises(TrackDownloadFailedError):
        await track.download()

    assert track.download_path.read_bytes() == b"the good copy"
    assert [p.name for p in tmp_path.iterdir()] == [track.download_path.name]


@pytest.mark.parametrize("expires", [True, False])
async def test_only_a_url_that_goes_stale_is_resolved_again(tmp_path, expires):
    # A YouTube watch url (Spotify) never expires: resolving it again only
    # searched YouTube Music once more for every track that waited its turn.
    track = _make_track(tmp_path, "flac")
    track.client = MagicMock(source="tidal")
    track.client.get_downloadable = AsyncMock(return_value=FakeDownloadable("flac"))
    track.downloadable.url_expires = expires
    track.downloadable.resolved_at = 0.0  # long ago
    await track.preprocess()

    await track.download()

    assert track.client.get_downloadable.await_count == int(expires)


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


async def test_a_track_waits_its_turn_right_before_its_stream(tmp_path):
    # Waiting before the metadata instead (on Tidal it comes with the album's
    # listing) made an album's tracks wait one by one, then stream all at once.
    database = db.Database(db.Downloads(str(tmp_path / "downloads.db")), db.Dummy())
    asked = []

    def ask(what):
        async def asking(*_):
            asked.append(what)
            if what == "metadata":
                raise RuntimeError("service down")

        return asking

    client = MagicMock()
    client.source = "tidal"
    client.get_metadata = ask("metadata")
    client.before_track = ask("turn")
    client.get_downloadable = ask("stream")

    await fetch_track_meta(client, database, "123")
    await fetch_downloadable(client, Config.defaults(), database, "123")

    assert asked == ["metadata", "turn", "stream"]


# --- albums -------------------------------------------------------------------


def _album_meta(album="Album"):
    meta = MagicMock()
    meta.album = album
    meta.info.container = "FLAC"
    meta.info.bit_depth = None
    meta.info.sampling_rate = None
    return meta


def _album(tracks):
    config = MagicMock()
    config.session.metadata.prefer_explicit = False
    return Album(
        meta=_album_meta(),
        tracks=tracks,
        config=config,
        folder=Path("/x"),
        db=MagicMock(),
    )


async def test_resolves_only_a_few_tracks_at_once():
    concurrent = peak = 0

    class Pending:
        async def look_up(self):
            return MagicMock()

        async def resolve(self):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0)
            concurrent -= 1
            track = MagicMock()
            track.rip = AsyncMock()
            return track

    await _album([Pending() for _ in range(RESOLVE_CONCURRENCY * 3)]).download()
    assert 1 < peak <= RESOLVE_CONCURRENCY


async def test_a_downloading_track_does_not_hold_a_resolve_slot():
    n = RESOLVE_CONCURRENCY * 3
    resolved = 0
    all_resolved = asyncio.Event()

    class FakeTrack:
        async def rip(self):
            # Only finishes once every track has been resolved, which can't
            # happen if downloads sit on the few resolve slots.
            await all_resolved.wait()

    class Pending:
        async def look_up(self):
            return MagicMock()

        async def resolve(self):
            nonlocal resolved
            await asyncio.sleep(0)
            resolved += 1
            if resolved == n:
                all_resolved.set()
            return FakeTrack()

    await asyncio.wait_for(_album([Pending() for _ in range(n)]).download(), 5)
    assert resolved == n


def _pending_album(monkeypatch, tmp_path, tracklist, downloaded, booklets=None):
    """A Tidal album "Encore" of `tracklist`, with the `downloaded` ones done."""
    meta = _album_meta("Encore")
    if booklets is not None:
        meta.info.booklets = booklets
    client = MagicMock()
    client.source = "tidal"
    client.get_metadata = AsyncMock(return_value={})
    database = MagicMock()
    database.skipped_now = 0
    database.downloaded.side_effect = lambda source, track_id: (
        source == "tidal" and track_id in downloaded
    )
    config = MagicMock()
    config.session.downloads.folder = str(tmp_path)
    artwork = AsyncMock(return_value=("cover.jpg", None))
    monkeypatch.setattr(
        album_module.AlbumMetadata, "from_album_resp", lambda resp, source: meta
    )
    monkeypatch.setattr(
        album_module, "get_album_track_ids", lambda source, resp: tracklist
    )
    monkeypatch.setattr(album_module, "download_artwork", artwork)
    monkeypatch.setattr(
        album_module, "album_folder", lambda config, source, m: tmp_path / "Encore"
    )
    return PendingAlbum("1", client, config, database), artwork


@pytest.mark.parametrize(
    ("downloaded", "said"),
    [
        (set(), None),
        ({"1"}, "Encore: skipping 1 of 3 tracks already downloaded"),
        ({"1", "2", "3"}, "Encore: all 3 tracks already downloaded, skipping"),
    ],
    ids=["new", "partly downloaded", "finished"],
)
async def test_an_album_says_once_what_it_skips(
    monkeypatch, tmp_path, caplog, downloaded, said
):
    pending, artwork = _pending_album(
        monkeypatch, tmp_path, ["1", "2", "3"], downloaded
    )

    album = await pending.resolve()

    assert [t.id for t in album.tracks] == sorted({"1", "2", "3"} - downloaded)
    # An artist resolves all its albums before its filters drop some: nothing
    # is counted, said or made for an album until it is wanted.
    assert pending.db.skipped_now == 0
    artwork.assert_not_awaited()
    assert not (tmp_path / "Encore").exists()
    assert [m for m in caplog.messages if "kipping" in m] == []

    await album.preprocess()

    assert pending.db.skipped_now == len(downloaded)
    assert [m for m in caplog.messages if "kipping" in m] == ([said] if said else [])
    # A finished album gets no folder or cover.
    assert artwork.await_count == (len(downloaded) < 3)
    assert (tmp_path / "Encore").is_dir() is (len(downloaded) < 3)
    assert all(t.cover_path == "cover.jpg" for t in album.tracks)


@pytest.mark.parametrize("create_album_folder", [True, False])
async def test_create_album_folder_controls_whether_artwork_can_be_saved(
    monkeypatch, tmp_path, create_album_folder
):
    pending, artwork = _pending_album(monkeypatch, tmp_path, ["1"], downloaded=set())
    pending.config.session.downloads.create_album_folder = create_album_folder

    await (await pending.resolve()).preprocess()

    assert artwork.call_args.kwargs["shared_folder"] is not create_album_folder


class _BookletDownloadable:
    downloaded: list[str] = []

    def __init__(self, _session, url, _extension):
        self.url = url

    async def download(self, path, _callback):
        if "broken" in self.url:
            path.write_bytes(b"%PD")  # cut off mid-download
            raise ConnectionError("gone")
        type(self).downloaded.append(self.url)
        with path.open("wb") as f:
            f.write(b"%PDF")


async def test_booklets_are_saved_as_pdfs(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(album_module, "BasicDownloadable", _BookletDownloadable)
    _BookletDownloadable.downloaded = []
    (tmp_path / "Liner Notes.pdf").write_bytes(b"%PDF")
    booklets = [
        {"description": "Digital Booklet", "url": "https://q/1.pdf"},
        {"name": "Poster", "url": "https://q/2.jpg"},  # not a booklet
        {"description": "Liner Notes", "url": "https://q/3.pdf"},  # already there
        {"description": "Lyrics", "url": "https://q/broken.pdf"},
        {"description": "Digital Booklet", "url": "https://q/5.pdf"},
    ]

    await album_module.download_booklets(None, booklets, tmp_path)

    assert _BookletDownloadable.downloaded == ["https://q/1.pdf", "https://q/5.pdf"]
    # Only names that would collide get a number.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "Digital Booklet 1.pdf",
        "Digital Booklet 4.pdf",
        "Liner Notes.pdf",
    ]
    assert "Could not download booklet https://q/broken.pdf" in caplog.text


@pytest.mark.parametrize(
    ("enabled", "in_album_folder", "downloaded", "fetched"),
    [
        (True, True, set(), 1),
        (False, True, set(), 0),
        # Booklets land beside the tracks; with no album folder to put them
        # in, they would collide with every other album's.
        (True, False, set(), 0),
        (True, True, {"1"}, 0),  # a finished album
    ],
    ids=["on", "off", "no album folder", "finished album"],
)
async def test_booklets_follow_the_config(
    monkeypatch, tmp_path, enabled, in_album_folder, downloaded, fetched
):
    booklets = [{"description": "Booklet", "url": "https://q/1.pdf"}]
    pending, _ = _pending_album(monkeypatch, tmp_path, ["1"], downloaded, booklets)
    pending.config.session.qobuz.download_booklets = enabled
    pending.config.session.downloads.create_album_folder = in_album_folder
    download = AsyncMock()
    monkeypatch.setattr(album_module, "download_booklets", download)
    monkeypatch.setattr(album_module, "progress", MagicMock())

    album = await pending.resolve()
    # Resolving alone downloads nothing: an album an artist or label filter
    # drops afterwards must not leave its booklets behind.
    download.assert_not_awaited()

    await album.preprocess()
    assert download.await_count == fetched


# --- artists and labels -------------------------------------------------------


def _filters(**on) -> ArtistFilterConfig:
    off = dict.fromkeys(
        ("extras", "repeats", "non_albums", "features", "non_remaster"), False
    )
    return ArtistFilterConfig(**(off | on))


NO_FILTERS = _filters()


def _artist(albums) -> Artist:
    return Artist(name="artist", albums=albums, client=None, config=None)


def _release(id, title, rate=44.1, depth=16, explicit=False) -> Album:
    """An album of the artist "artist", as a discography lists it."""
    info = AlbumInfo(
        id=id,
        quality=0,
        container="mp3",
        explicit=explicit,
        sampling_rate=rate,
        bit_depth=depth,
    )
    meta = AlbumMetadata(
        info=info,
        album=title,
        albumartist="artist",
        year="2020",
        genre=["genre"],
        covers=None,
        tracktotal=10,
    )
    return Album(meta=meta, tracks=[], config=None, folder="folder", db=None)


@pytest.mark.parametrize("method", ["_download_async", "_resolve_then_download"])
async def test_albums_are_resolved_and_ripped_one_at_a_time(method):
    # An album's tracks never mix with another's on screen, or on the rate
    # limit; with repeats on every album is resolved first, still one by one.
    busy = peak = 0

    async def one_at_a_time(result=None):
        nonlocal busy, peak
        busy += 1
        peak = max(peak, busy)
        await asyncio.sleep(0)  # so overlapping calls would interleave
        busy -= 1
        return result

    class FakeAlbum:
        async def rip(self):
            await one_at_a_time()

    class Pending:
        async def resolve(self):
            return await one_at_a_time(FakeAlbum())

    artist = _artist([Pending() for _ in range(4)])
    await asyncio.wait_for(getattr(artist, method)(NO_FILTERS), 5)

    assert peak == 1


@pytest.mark.parametrize(
    ("run", "fails_in"),
    [
        (lambda albums: _artist(albums)._download_async(NO_FILTERS), "resolve"),
        (lambda albums: _artist(albums)._resolve_then_download(NO_FILTERS), "resolve"),
        (lambda albums: Label("label", albums, None, None).download(), "rip"),
    ],
    ids=["artist", "artist, repeats on", "label"],
)
async def test_a_failing_album_does_not_stop_the_others(caplog, run, fails_in):
    ripped = []

    class FakeAlbum:
        def __init__(self, n):
            self.n = n

        async def rip(self):
            if self.n == 2 and fails_in == "rip":
                raise ConnectionError("boom")
            ripped.append(self.n)

    class Pending:
        def __init__(self, n):
            self.n = n

        async def resolve(self):
            if self.n == 2 and fails_in == "resolve":
                raise ConnectionError("boom")
            return FakeAlbum(self.n)

    await run([Pending(n) for n in (1, 2, 3)])

    assert sorted(ripped) == [1, 3]
    assert "ConnectionError: boom" in caplog.text


@pytest.mark.parametrize(
    ("albums", "kept"),
    [
        ([_release("a1", "Test Album")], ["a1"]),
        (
            [
                _release("a1", "Test Album"),
                _release("a2", "Another Album", 96, 24, explicit=True),
            ],
            ["a1", "a2"],
        ),
        # Of two editions a higher bit depth wins, then a higher sampling rate,
        # then the explicit one, also when no quality is known.
        (
            [
                _release("a1", "Test Album"),
                _release("a2", "Test Album (Deluxe)", 44.1, 24),
            ],
            ["a2"],
        ),
        (
            [
                _release("a1", "Test Album", 44.1, 24),
                _release("a2", "Test Album (Live)", 96, 24),
            ],
            ["a2"],
        ),
        (
            [
                _release("a1", "Test Album", 96, 24),
                _release("a2", "Test Album (Edited)", 96, 24, explicit=True),
            ],
            ["a2"],
        ),
        (
            [
                _release("a1", "Test Album", None, None),
                _release("a2", "Test Album", None, None, explicit=True),
            ],
            ["a2"],
        ),
        # Titles alike up to a bracket are editions of one album.
        (
            [
                _release("a1", "Album X"),
                _release("a2", "Album X (Deluxe)", 96, 24),
                _release("a3", "Album X [Special Edition]", explicit=True),
            ],
            ["a2"],
        ),
        (
            [
                _release("a1", "Album A"),
                _release("a2", "Album A (Remastered)", 96, 24, explicit=True),
                _release("b1", "Album B", 96, 24),
                _release("b2", "Album B (Live)", explicit=True),
                _release("c1", "Album C", None, None),
            ],
            ["a2", "b1", "c1"],
        ),
        # A title that starts with a bracket used to abort the artist download.
        (
            [
                _release("a1", "(What's the Story) Morning Glory?"),
                _release("a2", "[Untitled]"),
                _release("a3", "(What's the Story) Morning Glory?", 96, 24),
            ],
            ["a2", "a3"],
        ),
    ],
)
def test_the_repeats_filter_keeps_the_best_edition_of_an_album(albums, kept):
    assert sorted(a.meta.info.id for a in Artist._filter_repeats(albums)) == kept


@pytest.mark.parametrize(
    ("only", "changes", "wanted"),
    [
        ("non_albums", {"tracktotal": 1}, False),
        ("non_albums", {}, True),
        ("extras", {"albumartist": "Various Artists"}, False),
        ("extras", {}, True),
        # An album the artist shares is not a feature.
        (
            "features",
            {"albumartist": "artist, Other", "albumartists": ["artist", "Other"]},
            True,
        ),
        ("features", {"albumartist": "Someone Else"}, False),
        ("features", {}, True),
    ],
)
def test_an_artist_filter_drops_what_it_names(only, changes, wanted):
    album = _release("a", "Album")
    for name, value in changes.items():
        setattr(album.meta, name, value)
    assert _artist([])._wanted(album, _filters(**{only: True})) is wanted


async def test_repeats_choose_among_what_the_other_filters_keep(monkeypatch):
    ripped = []

    async def rip(self):
        ripped.append(self.meta.info.id)

    class Pending:
        def __init__(self, album):
            self.album = album

        async def resolve(self):
            return self.album

    monkeypatch.setattr(Album, "rip", rip)
    plain = _release("plain", "Album X")
    deluxe = _release("deluxe", "Album X (Deluxe Edition)", 96, 24)
    artist = _artist([Pending(plain), Pending(deluxe)])

    await artist._resolve_then_download(_filters(extras=True, repeats=True))

    assert ripped == ["plain"]


@pytest.mark.parametrize("resolve_first", [False, True])
@pytest.mark.parametrize("selected", [False, True])
async def test_artist_counts_skips_only_for_selected_albums(
    monkeypatch, tmp_path, resolve_first, selected
):
    # Filtering an album out after it is resolved must not count its skips.
    pending, _ = _pending_album(monkeypatch, tmp_path, ["1", "2"], {"1", "2"})
    pending.config.session.cli.progress_bars = False
    filters = pending.config.session.artist_filters
    filters.repeats = False
    monkeypatch.setattr(Artist, "_wanted", lambda self, album, filters: selected)
    artist = Artist("Artist", [pending], pending.client, pending.config)
    if resolve_first:
        await artist._resolve_then_download(filters)
    else:
        await artist._download_async(filters)
    assert pending.db.skipped_now == (2 if selected else 0)


# --- prefer_explicit ----------------------------------------------------------


class _Resolved:
    """A resolved Track as filter_prefer_explicit sees it: only its `.meta`."""

    def __init__(self, title, artist, explicit):
        album = AlbumMetadata(
            AlbumInfo("1", 2, "flac"), "Album", artist, "2020", [], Covers(), 1
        )
        self.meta = TrackMetadata(
            info=TrackInfo(id="1", explicit=explicit),
            title=title,
            album=album,
            artist=artist,
            tracknumber=1,
            discnumber=1,
            composer=None,
        )


@pytest.mark.parametrize(
    ("tracks", "kept"),
    [
        ([("Song", "Artist", False), ("Song", "Artist", True)], [1]),
        # The same song in another case, or with spaces around it.
        ([("  song  ", "ARTIST", False), ("Song", "artist", True)], [1]),
        ([("Song", "Artist", False)], [0]),
        ([("Song", "Artist", True)], [0]),
        (
            [
                ("Song", "Artist", False),
                ("Song", "Artist", True),
                ("Other Song", "Artist", False),
            ],
            [1, 2],
        ),
    ],
)
def test_prefer_explicit_drops_only_the_clean_copy_of_a_song(tracks, kept):
    resolved = list(starmap(_Resolved, tracks))
    assert filter_prefer_explicit(resolved) == [resolved[i] for i in kept]


# --- playlists ----------------------------------------------------------------


def _lastfm_playlist(monkeypatch, total_tracks, fail_page=None):
    session = AsyncMock()
    session.__aenter__.return_value = session
    active_requests = 0

    @asynccontextmanager
    async def get(url, **kwargs):
        nonlocal active_requests
        active_requests += 1
        try:
            # A suspended response must not allow more page requests to start.
            await asyncio.sleep(0)
            assert active_requests == 1
            page = kwargs.get("params", {}).get("page", 1)
            if page == fail_page:
                raise ConnectionError("page unavailable")
            start = (page - 1) * 50
            tracks = "".join(
                f'<a href="/track" title="Song {i} &amp; Co">'
                '<a href="/artist" title="Artist &amp; Co">'
                for i in range(start, min(start + 50, total_tracks))
            )
            response = AsyncMock()
            response.text.return_value = (
                '<h1 class="playlisting-playlist-header-title">Mix &amp; Match</h1>'
                f'<div data-playlisting-entry-count="{total_tracks}"></div>{tracks}'
            )
            yield response
        finally:
            active_requests -= 1

    session.get = MagicMock(side_effect=get)
    monkeypatch.setattr("streamrip.media.playlist.new_session", lambda **kw: session)
    playlist = PendingLastfmPlaylist(
        "https://www.last.fm/playlist/test",
        MagicMock(),
        None,
        Config.defaults(),
        MagicMock(),
    )
    return playlist, session


@pytest.mark.parametrize("count", [LASTFM_MAX_TRACKS + 1, 1_000_000_000])
async def test_lastfm_rejects_excessive_counts_before_pagination(
    monkeypatch, caplog, count
):
    playlist, session = _lastfm_playlist(monkeypatch, count)

    assert await playlist.resolve() is None

    assert "supported limit" in caplog.text
    playlist.client.search.assert_not_called()
    session.get.assert_called_once_with(playlist.lastfm_url)
    session.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("count", [0, 1, 50, 51, 100, 101, LASTFM_MAX_TRACKS])
async def test_lastfm_pagination_is_bounded_and_ordered(monkeypatch, count):
    playlist, session = _lastfm_playlist(monkeypatch, count)

    title, tracks = await playlist._parse_lastfm_playlist(playlist.lastfm_url)

    assert title == "Mix & Match"
    assert tracks == [(f"Song {i} & Co", "Artist & Co") for i in range(count)]
    calls = session.get.call_args_list
    assert len(calls) == max(1, (count + 49) // 50)
    assert [call.kwargs for call in calls] == [{}] + [
        {"params": {"page": i}} for i in range(2, len(calls) + 1)
    ]
    assert all(call.args == (playlist.lastfm_url,) for call in calls)
    session.__aexit__.assert_awaited_once()


async def test_lastfm_page_failure_stops_pagination_and_closes_session(monkeypatch):
    playlist, session = _lastfm_playlist(monkeypatch, 151, fail_page=2)

    with pytest.raises(ConnectionError, match="page unavailable"):
        await playlist._parse_lastfm_playlist(playlist.lastfm_url)

    assert session.get.call_count == 2
    session.__aexit__.assert_awaited_once()


async def test_lastfm_tracks_are_searched_like_a_csv_lists(monkeypatch):
    # A Last.fm page names a title and an artist per track; they are found the
    # way a CSV list's are (below), not by the first search hit.
    searched = AsyncMock(return_value=None)
    monkeypatch.setattr("streamrip.media.playlist.searched_playlist", searched)
    playlist = PendingLastfmPlaylist("url", "client", "fallback", "config", "db")
    playlist._parse_lastfm_playlist = AsyncMock(
        return_value=("Mix", [("Song", "Band"), ("Other", "Act")])
    )

    await playlist.resolve()

    name, tracks, *rest = searched.await_args.args
    assert (name, rest) == ("Mix", ["client", "fallback", "config", "db"])
    assert [(t.title, t.artists) for t in tracks] == [
        ("Song", ["Band"]),
        ("Other", ["Act"]),
    ]


@pytest.mark.parametrize(
    "get_metadata",
    [
        AsyncMock(side_effect=NonStreamableError("gone")),
        AsyncMock(return_value={"unexpected": "shape"}),
    ],
    ids=["cannot be fetched", "unreadable metadata"],
)
async def test_a_playlist_track_that_fails_is_kept_for_repair(get_metadata):
    client = MagicMock()
    client.source = "tidal"
    client.get_metadata = get_metadata
    client.before_track = AsyncMock()
    database = MagicMock()
    database.downloaded.return_value = False
    track = PendingPlaylistTrack("42", client, MagicMock(), "/x", "Mix", 1, database)

    assert await track.resolve() is None
    database.set_failed.assert_called_once_with("tidal", "track", "42")


def _deezer_track(position, disc):
    return {
        "id": 7,
        "title": "Song",
        "artist": {"name": "Artist"},
        "contributors": [{"name": "Artist", "type": "artist"}],
        "track_position": position,
        "disk_number": disc,
        "album": {
            "id": 70,
            "title": "Their Own Album",
            "release_date": "2020-01-01",
            **{
                f"cover_{s}": "https://c/x.jpg"
                for s in ("xl", "big", "medium", "small")
            },
        },
    }


async def test_playlist_track_is_tagged_as_part_of_one_album(monkeypatch):
    monkeypatch.setattr(
        "streamrip.media.playlist.download_artwork",
        AsyncMock(return_value=(None, None)),
    )
    client = MagicMock()
    client.source = "deezer"
    client.get_metadata = AsyncMock(return_value=_deezer_track(position=5, disc=2))
    client.get_downloadable = AsyncMock()
    client.before_track = AsyncMock()
    database = MagicMock()
    database.downloaded.return_value = False
    pending = PendingPlaylistTrack(
        "7", client, Config.defaults(), "/x", "Road Trip", 3, database, total=50
    )

    meta = (await pending.resolve()).meta

    assert (meta.tracknumber, meta.discnumber) == (3, 1)
    album = meta.album
    assert (album.tracktotal, album.disctotal) == (50, 1)
    # One album artist for the whole playlist, or servers split it per artist.
    assert (album.album, album.albumartist, album.compilation) == (
        "Road Trip",
        "Various Artists",
        "1",
    )


async def test_a_track_listed_twice_in_a_playlist_is_downloaded_once(tmp_path):
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path)
    client = MagicMock()
    client.source = "tidal"
    tracks = [{"id": 1}, {"id": 2}, {"id": 1}, {"id": 3}]
    client.get_metadata = AsyncMock(return_value={"title": "Mix", "tracks": tracks})

    playlist = await PendingPlaylist("1", client, config, MagicMock()).resolve()

    assert [(t.id, t.position) for t in playlist.tracks] == [
        ("1", 1),
        ("2", 2),
        ("3", 4),
    ]


def test_tracks_uploaded_to_deezer_are_left_out_of_a_playlist():
    # They have negative ids.
    resp = {"title": "Mix", "tracks": [{"id": 1}, {"id": -5}, {"id": "2"}]}
    assert PlaylistMetadata.from_deezer(resp).ids == ["1", "2"]


@pytest.mark.parametrize(
    ("restrict", "folder"), [(False, "/music/Café  Mix"), (True, "/music/Caf  Mix")]
)
def test_playlist_folder_is_one_folder_and_follows_restrict_characters(
    restrict, folder
):
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.filepaths.restrict_characters = restrict
    assert _playlist_folder(config, "Café / Mix") == Path(folder)


@pytest.mark.parametrize("name", ["", ".", "..", "/", ".. /", "é"])
def test_playlist_folder_rejects_special_components_after_sanitizing(tmp_path, name):
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path)
    config.session.filepaths.restrict_characters = True
    with pytest.raises(ValueError, match="playlist folder"):
        _playlist_folder(config, name)


@pytest.mark.parametrize("destination", ["outside", "root"])
def test_playlist_folder_rejects_symlinks_outside_or_to_root(tmp_path, destination):
    root = tmp_path / "music"
    root.mkdir()
    outside = tmp_path / "music-other"
    outside.mkdir()
    (root / "Mix").symlink_to(outside if destination == "outside" else root)
    config = Config.defaults()
    config.session.downloads.folder = str(root)
    with pytest.raises(ValueError, match="playlist folder"):
        _playlist_folder(config, "Mix")


@pytest.mark.parametrize("name", ["..", "Mix"])
async def test_remote_playlist_title_is_checked_before_tracks_are_created(
    tmp_path, monkeypatch, name
):
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path / "music")
    config.session.cli.progress_bars = False
    client = MagicMock()
    client.source = "tidal"
    client.get_metadata = AsyncMock(return_value={"title": name, "tracks": [{"id": 1}]})
    pending = PendingPlaylist("1", client, config, MagicMock())
    api_playlist = await pending.resolve()

    page = (
        f'<h1 class="playlisting-playlist-header-title">{name}</h1>'
        '<div data-playlisting-entry-count="1"></div>'
        '<a href="/track" title="Song"><a href="/artist" title="Artist">'
    )
    session = MagicMock()
    session.get.return_value.__aenter__.return_value.text = AsyncMock(return_value=page)
    monkeypatch.setattr(
        "streamrip.media.playlist.new_session",
        MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=session))),
    )
    query = AsyncMock(return_value=(client, MagicMock(id="1")))
    monkeypatch.setattr("streamrip.media.playlist.find_track", query)
    lastfm_playlist = await PendingLastfmPlaylist(
        "https://example.test/playlist", client, None, config, MagicMock()
    ).resolve()

    for playlist in (api_playlist, lastfm_playlist):
        if name == "..":
            assert playlist is None
        else:
            assert playlist.tracks[0].folder == tmp_path / "music" / "Mix"
    if name == "..":
        query.assert_not_awaited()
    assert not (tmp_path / "music").exists()


def _served_from(requested, served):
    """A Deezer client serving `requested` through the track `served`."""

    def track(id, title, cover):
        resp = _deezer_track(position=1, disc=1)
        resp.update(id=int(id), title=title)
        resp["album"] = {
            **resp["album"],
            "title": f"{title} album",
            **{f"cover_{s}": cover for s in ("xl", "big", "medium", "small")},
        }
        return resp

    responses = {
        served: track(served, "Served", "https://c/real-cover.jpg"),
        # Last, so it wins when the track is served as requested.
        requested: track(requested, "Requested", "https://c/placeholder.jpg"),
    }
    client = MagicMock()
    client.source = "deezer"
    client.get_metadata = AsyncMock(
        side_effect=lambda item_id, _type: responses[item_id]
    )
    client.get_downloadable = AsyncMock(
        return_value=MagicMock(id=served, source="deezer")
    )
    client.before_track = AsyncMock()
    database = MagicMock()
    database.downloaded.return_value = False
    config = Config.defaults()
    config.session.metadata.set_playlist_to_album = False
    return PendingPlaylistTrack(requested, client, config, "/x", "Mix", 1, database)


@pytest.fixture
def artwork_fetched(monkeypatch):
    artwork = AsyncMock(return_value=(None, None))
    monkeypatch.setattr("streamrip.media.playlist.download_artwork", artwork)
    return artwork


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("served", "title", "cover"),
    [
        ("99", "Served", "https://c/real-cover.jpg"),
        ("7", "Requested", "https://c/placeholder.jpg"),
    ],
    ids=["from a fallback", "as requested"],
)
async def test_a_playlist_track_takes_the_metadata_of_the_track_served(
    artwork_fetched, served, title, cover
):
    """A geoblocked or delisted Deezer track is served from FALLBACK.SNG_ID's release.

    Its own metadata no longer describes the file, and its cover is Deezer's
    grey placeholder (3 of 9 such tracks in a real 60-track playlist).
    """
    pending = _served_from("7", served)

    track = await pending.resolve()

    assert (track.meta.title, track.meta.album.album) == (title, f"{title} album")
    # One cover, the served release's: the placeholder is not even fetched.
    covers = [c.args[2].get_size("large")[1] for c in artwork_fetched.await_args_list]
    assert covers == [cover]
    assert pending.client.get_metadata.await_count == (1 if served == "7" else 2)
    pending.db.set_failed.assert_not_called()


@pytest.mark.asyncio
async def test_unreadable_served_track_keeps_the_requested_metadata(artwork_fetched):
    pending = _served_from("7", "99")
    first = pending.client.get_metadata.side_effect

    async def failing_for_served(id, _type):
        if id == "99":
            raise NonStreamableError("gone")
        return first(id, _type)

    pending.client.get_metadata = AsyncMock(side_effect=failing_for_served)

    track = await pending.resolve()

    assert track.meta.title == "Requested"
    # The served id is not the database's business: nothing recorded for it.
    pending.db.set_failed.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_served_track_is_recorded_under_the_requested_id(
    artwork_fetched, monkeypatch
):
    # Re-running the playlist must skip it, as it did before the served metadata.
    monkeypatch.setattr("streamrip.media.track.tag_file", AsyncMock())
    pending = _served_from("7", "99")

    track = await pending.resolve()
    await track.postprocess()

    assert track.meta.title == "Served"  # tags still come from the served track
    pending.db.set_downloaded.assert_called_once_with("deezer", "7")


# --- CSV lists: reading -------------------------------------------------------


def _write(tmp_path, text, encoding="utf-8"):
    path = tmp_path / "list.csv"
    path.write_bytes(text.encode(encoding))
    return path


def _rows(tracks):
    return [(t.title, t.artists, t.album, t.duration_ms) for t in tracks]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            (
                "title,artists,album,duration_ms,isrc,spotify_uri,tidal_id\n"
                "Get Lucky,Daft Punk; Pharrell Williams,Random Access Memories,369000,"
                "GBDUW1300136,spotify:track:x,\n"
                "Billie Jean,Michael Jackson,Thriller,293000.0,,,\n"
            ),
            [
                (
                    "Get Lucky",
                    ["Daft Punk", "Pharrell Williams"],
                    "Random Access Memories",
                    369000,
                ),
                ("Billie Jean", ["Michael Jackson"], "Thriller", 293000),
            ],
        ),
        # A semicolon between artists and a short row leave a file of commas alone.
        (
            (
                "title,artists,album\n"
                "Get Lucky,Daft Punk; Pharrell Williams,Random Access Memories\n"
                "Song,Band\n"
            ),
            [
                (
                    "Get Lucky",
                    ["Daft Punk", "Pharrell Williams"],
                    "Random Access Memories",
                    None,
                ),
                ("Song", ["Band"], "", None),
            ],
        ),
        # A comma inside a quoted title, and Duration (ms).
        (
            (
                "Track URI,Track Name,Album Name,Artist Name(s),Duration (ms)\n"
                'spotify:track:a,"Hello, Goodbye",Magical Mystery Tour,The Beatles,208000\n'
            ),
            [("Hello, Goodbye", ["The Beatles"], "Magical Mystery Tour", 208000)],
        ),
        (
            (
                "Track name,Artist name,Album,Playlist name,Type,ISRC\n"
                "Imagine,John Lennon,Imagine,Mix,Playlist,GBAYE7000001\n"
            ),
            [("Imagine", ["John Lennon"], "Imagine", None)],
        ),
        # Excel in many countries: semicolons, and a length as m:ss.
        (
            "Title;Artist;Album;Length\nWaterloo;ABBA;Waterloo;2:45\n",
            [("Waterloo", ["ABBA"], "Waterloo", 165_000)],
        ),
        ("title\tartist\nSong\tBand\n", [("Song", ["Band"], "", None)]),
        (
            "Queen,Bohemian Rhapsody\nABBA,Dancing Queen\n",
            [
                ("Bohemian Rhapsody", ["Queen"], "", None),
                ("Dancing Queen", ["ABBA"], "", None),
            ],
        ),
        # Excel for Mac's "CSV (Macintosh)" ends a line in \r alone.
        (
            "artist,title\rQueen,Bohemian Rhapsody\r",
            [("Bohemian Rhapsody", ["Queen"], "", None)],
        ),
        # A comma in a name, quoted or not, ties with the semicolon of its row:
        # it does not make the commas the separator.
        (
            '"Earth, Wind & Fire";September\n"Daft Punk";Get Lucky\n',
            [
                ("September", ["Earth, Wind & Fire"], "", None),
                ("Get Lucky", ["Daft Punk"], "", None),
            ],
        ),
        (
            "Earth, Wind & Fire;September\nDaft Punk;Get Lucky\n",
            [
                ("September", ["Earth, Wind & Fire"], "", None),
                ("Get Lucky", ["Daft Punk"], "", None),
            ],
        ),
        (
            (
                "title;artists\nSeptember;Earth, Wind & Fire\n"
                "Ohio;Crosby, Stills, Nash & Young\n"
            ),
            [
                ("September", ["Earth, Wind & Fire"], "", None),
                ("Ohio", ["Crosby, Stills, Nash & Young"], "", None),
            ],
        ),
        # Excel's byte order mark does not hide the header.
        ("﻿Title;Artist\nSong;Band\n", [("Song", ["Band"], "", None)]),
        (
            "title,artists\n\n,Nobody\nSong,Somebody\n , \n",
            [("Song", ["Somebody"], "", None)],
        ),
        ("", []),
        ("title,artists\n", []),
    ],
    ids=[
        "the csv of Music-Sync",
        "semicolon between artists, short row",
        "exportify",
        "tunemymusic",
        "semicolons",
        "tabs",
        "no header",
        "mac",
        "quoted comma in a name",
        "comma in a name",
        "commas in names, header without",
        "byte order mark",
        "blank rows and rows without a title",
        "empty",
        "only a header",
    ],
)
def test_a_csv_list_is_read(tmp_path, text, expected):
    assert _rows(read_tracks(_write(tmp_path, text))) == expected


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        ("duration_ms\n123456.0\n", 123456),
        ("duration_ms\nnot a number\n", None),
        ("duration\n3:45\n", 225_000),
        ("length\n1:02:03\n", 3_723_000),
        # A bare number could be seconds or milliseconds: better to say nothing.
        ("duration\n225\n", None),
        ("duration\n225000\n", None),
    ],
)
def test_a_length_is_only_taken_when_its_unit_is_clear(tmp_path, duration, expected):
    header, value = duration.split("\n")[:2]
    path = _write(tmp_path, f"title,{header}\nSong,{value}\n")

    assert read_tracks(path)[0].duration_ms == expected


def test_a_file_that_is_not_utf8_is_refused_with_advice(tmp_path):
    path = _write(tmp_path, "title,artists\nCafé,Band\n", encoding="latin-1")

    with pytest.raises(ValueError, match="UTF-8"):
        read_tracks(path)


# --- CSV lists: finding the tracks --------------------------------------------

WANTED = MatchTrack(
    "Get Lucky (feat. Pharrell Williams)",
    ["Daft Punk", "Pharrell Williams"],
    "Random Access Memories",
    369_000,
)


def _summary(name, artist, album="", length=None, id="1"):
    details = [("Album", album), ("Length", length)]
    return Summary("track", id, name, artist, details=[d for d in details if d[1]])


@pytest.mark.parametrize(
    ("results", "chosen"),
    [
        (
            [
                _summary("Get Lucky - Live", "Daft Punk", id="live"),
                _summary("Get Lucky", "Daft Punk, Pharrell Williams", id="right"),
                _summary("Another Song", "Daft Punk", id="other"),
            ],
            "right",
        ),
        (
            [
                _summary("Get Lucky", "Daft Punk", "RAM", "4:08", id="edit"),
                _summary(
                    "Get Lucky", "Daft Punk", "Random Access Memories", "6:09", id="lp"
                ),
            ],
            "lp",
        ),
        ([], None),
        ([_summary("Get Lucky", "Somebody Else")], None),
        ([_summary("Get Lucky (Karaoke Version)", "Daft Punk")], None),
        ([_summary("Something Else Entirely", "Daft Punk")], None),
    ],
    ids=[
        "the track",
        "the edit of its length",
        "nothing",
        "another artist",
        "another version",
        "another song",
    ],
)
def test_the_search_result_that_is_the_track_is_chosen(results, chosen):
    hit = best_result(WANTED, results)
    assert (hit[0].id if hit else None) == chosen
    assert hit is None or hit[1] >= 0.9  # its score: a sure match


@pytest.mark.parametrize(
    ("wanted", "searched"),
    [
        # Without brackets first; the title before the artist last.
        (
            WANTED,
            [
                "Daft Punk Get Lucky",
                "Daft Punk Get Lucky (feat. Pharrell Williams)",
                "Get Lucky Daft Punk",
            ],
        ),
        (MatchTrack("Song", ["Band"]), ["Band Song", "Song Band"]),
        (MatchTrack("Song"), ["Song"]),  # no artist, no gap
    ],
)
def test_what_is_searched_for_a_track(wanted, searched):
    assert queries(wanted) == searched


class Catalogue:
    """A source that finds tracks by a table of query -> results."""

    def __init__(self, source, table, fail=()):
        self.source = source
        self.table = table
        self.fail = fail
        self.asked = []

    async def search(self, media_type, query, limit=50, offset=0):
        self.asked.append((media_type, query, limit))
        if query in self.fail:
            raise ConnectionError("down")
        return [{"items": self.table.get(query, [])}]


def _item(id, title, artist="Band"):
    return {
        "id": id,
        "title": title,
        "artists": [{"name": artist}],
        "album": {"title": "Album"},
        "duration": 200,
    }


def _csv_playlist(tmp_path, tracks, client, fallback=None, name="Mix"):
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path)
    config.session.cli.progress_bars = False
    return PendingCsvPlaylist(name, tracks, client, fallback, config, MagicMock())


ONE = MatchTrack("Song One", ["Band"], duration_ms=200_000)
TWO = MatchTrack("Song Two", ["Band"], duration_ms=200_000)
THREE = MatchTrack("Song Three", ["Band"], duration_ms=200_000)
HAS_ONE = {"Band Song One": [_item(11, "Song One")]}
HAS_TWO = {"Band Song Two": [_item(22, "Song Two")]}


async def test_the_tracks_found_become_a_playlist_in_a_folder_of_the_lists_name(
    tmp_path,
):
    tidal = Catalogue("tidal", HAS_ONE | {"Band Song Three": [_item(33, "Song Three")]})

    playlist = await _csv_playlist(tmp_path, [ONE, TWO, THREE], tidal).resolve()

    assert playlist is not None
    assert playlist.name == "Mix"
    assert [(t.id, t.position, t.total) for t in playlist.tracks] == [
        ("11", 1, 3),
        ("33", 3, 3),  # the position in the list, also where one is missing
    ]
    assert {t.folder for t in playlist.tracks} == {tmp_path / "Mix"}
    assert all(t.client is tidal for t in playlist.tracks)
    assert ("track", "Band Song One", 5) in tidal.asked  # five results are looked at


@pytest.mark.parametrize(
    ("tracks", "tidal", "fallback", "found", "said"),
    [
        (
            [ONE, TWO],
            Catalogue("tidal", HAS_ONE),
            Catalogue("spotify", HAS_TWO),
            [("11", 1, "tidal"), ("22", 2, "spotify")],
            None,
        ),
        (
            [ONE, TWO],
            Catalogue("tidal", HAS_ONE),
            None,
            [("11", 1, "tidal")],
            "Not found: Band - Song Two",
        ),
        (
            [ONE, TWO, ONE],
            Catalogue("tidal", HAS_ONE | HAS_TWO),
            None,
            [("11", 1, "tidal"), ("22", 2, "tidal")],
            None,
        ),
        (
            [ONE, TWO],
            Catalogue("tidal", HAS_TWO, fail={"Band Song One"}),
            None,
            [("22", 2, "tidal")],
            None,
        ),
        (
            [ONE],
            Catalogue("tidal", {"Band Song One": [_item(99, "Song One (Live)")]}),
            None,
            None,
            None,
        ),
        (
            [ONE, TWO],
            Catalogue("tidal", {}),
            None,
            None,
            "None of the tracks of Mix was found",
        ),
    ],
    ids=[
        "the fallback has what the source lacks",
        "a track that is nowhere",
        "a track listed twice",
        "a search that fails",
        "a wrong result",
        "nothing found",
    ],
)
async def test_the_tracks_of_a_list_are_found_where_they_are(
    tmp_path, caplog, tracks, tidal, fallback, found, said
):
    playlist = await _csv_playlist(tmp_path, tracks, tidal, fallback).resolve()

    if found is None:
        assert playlist is None
    else:
        assert [(t.id, t.position, t.client.source) for t in playlist.tracks] == found
    if said:
        assert said in caplog.text


async def test_the_full_title_is_tried_when_the_short_one_finds_nothing(tmp_path):
    wanted = MatchTrack("Song (feat. Guest)", ["Band"], duration_ms=200_000)
    tidal = Catalogue(
        "tidal", {"Band Song (feat. Guest)": [_item(5, "Song (feat. Guest)")]}
    )

    playlist = await _csv_playlist(tmp_path, [wanted], tidal).resolve()

    assert [q for _, q, _ in tidal.asked] == ["Band Song", "Band Song (feat. Guest)"]
    assert [t.id for t in playlist.tracks] == ["5"]


@pytest.mark.parametrize("name", [".", "..", ""])
async def test_a_list_name_that_is_no_folder_is_refused(tmp_path, name):
    tidal = Catalogue("tidal", HAS_ONE)

    playlist = await _csv_playlist(tmp_path, [ONE], tidal, name=name).resolve()

    assert playlist is None
    assert tidal.asked == []


async def test_the_progress_shows_what_is_found(tmp_path, monkeypatch):
    spinner = MagicMock()

    @contextmanager
    def status(text, **_):
        yield spinner

    monkeypatch.setattr("streamrip.media.playlist.console.status", status)
    tidal = Catalogue("tidal", HAS_ONE)
    playlist = _csv_playlist(tmp_path, [ONE, TWO], tidal)
    playlist.config.session.cli.progress_bars = True

    await playlist.resolve()

    shown = [call.args[0].plain for call in spinner.update.call_args_list]
    assert shown[-1] == "Searching Tidal for the tracks (1 found, 1 not found, 2 total)"


# --- artwork ------------------------------------------------------------------


@pytest.mark.parametrize("for_playlist", [False, True])
@pytest.mark.parametrize("download_fails", [False, True])
async def test_artwork_cleanup_preserves_existing_files(
    tmp_path, monkeypatch, for_playlist, download_fails
):
    folder = tmp_path / "Mix"
    legacy = folder / "__artwork"
    legacy.mkdir(parents=True)
    kept = legacy / "keep.txt"
    kept.write_text("existing artwork")
    monkeypatch.setattr(artwork, "_artwork_tempdirs", set())
    downloaded = []

    async def download(self, path, callback):
        path.write_bytes(b"image")
        downloaded.append(path)
        if download_fails:
            raise OSError("download interrupted")

    monkeypatch.setattr(artwork.BasicDownloadable, "download", download)
    config = Config.defaults().session.artwork
    config.embed = True
    config.save_artwork = True
    config.embed_max_width = config.saved_max_width = 0

    for _ in range(2):
        covers = Covers()
        covers.set_cover("large", "https://example.test/cover.jpg", None)
        await artwork.download_artwork(
            MagicMock(), folder, covers, config, for_playlist
        )

    owned = set(artwork._artwork_tempdirs)
    artwork.remove_artwork_tempdirs()
    assert kept.read_text() == "existing artwork"
    assert len(owned) == 2
    assert all(not path.exists() for path in owned)
    assert not artwork._artwork_tempdirs
    assert all(path.parent != legacy for path in downloaded)
    if not for_playlist:
        # A download that failed leaves no cover.jpg it may have cut off.
        assert (folder / "cover.jpg").exists() is not download_fails

    # A later cleanup must not delete a path recreated after the first cleanup.
    recreated = owned.pop()
    recreated.mkdir()
    artwork.remove_artwork_tempdirs()
    assert recreated.is_dir()


@pytest.mark.parametrize(
    ("failures", "saved", "tries"),
    [
        ([requests.ConnectionError("interrupted download")], True, 2),
        ([requests.exceptions.ContentDecodingError("interrupted download")], True, 2),
        ([requests.HTTPError(response=MagicMock(status_code=404))], False, 1),
        (
            [requests.ConnectionError("down")] * artwork.ARTWORK_ATTEMPTS,
            False,
            artwork.ARTWORK_ATTEMPTS,
        ),
    ],
    ids=["connection", "decoding", "missing: not retried", "keeps failing"],
)
async def test_a_network_hiccup_does_not_cost_the_album_its_cover(
    tmp_path, monkeypatch, failures, saved, tries
):
    pending = list(failures)
    calls = []

    async def download(self, path, callback):
        calls.append(path)
        if pending:
            path.write_bytes(b"ima")  # cut off mid-download
            raise pending.pop(0)
        path.write_bytes(b"image")

    monkeypatch.setattr(artwork.BasicDownloadable, "download", download)
    monkeypatch.setattr(artwork.asyncio, "sleep", AsyncMock())
    config = Config.defaults().session.artwork
    config.embed = False
    config.save_artwork = True
    config.saved_max_width = 0
    covers = Covers()
    covers.set_cover("large", "https://example.test/cover.jpg", None)

    result = await artwork.download_artwork(
        MagicMock(), tmp_path, covers, config, False
    )

    cover = tmp_path / "cover.jpg"
    assert result == (None, cover if saved else None)
    assert len(calls) == tries
    # Never a cut-off image left in its place.
    assert (cover.read_bytes() if cover.exists() else None) == (
        b"image" if saved else None
    )


async def test_a_saved_cover_that_fails_leaves_the_embedded_one(tmp_path, monkeypatch):
    async def download(self, path, callback):
        if path.name == "cover.jpg":  # Qobuz's derived original size, a 404
            raise requests.HTTPError(response=MagicMock(status_code=404))
        path.write_bytes(b"image")

    monkeypatch.setattr(artwork.BasicDownloadable, "download", download)
    monkeypatch.setattr(artwork, "_artwork_tempdirs", set())
    config = Config.defaults().session.artwork
    config.embed = config.save_artwork = True
    config.embed_max_width = config.saved_max_width = 0
    covers = Covers()
    covers.set_cover("original", "https://example.test/org.jpg", None)
    covers.set_cover("large", "https://example.test/600.jpg", None)

    embedded, saved = await artwork.download_artwork(
        MagicMock(), tmp_path, covers, config, False
    )

    assert saved is None
    assert embedded.read_bytes() == b"image"
    artwork.remove_artwork_tempdirs()
