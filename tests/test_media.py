"""Tracks, albums, artists, labels and playlists: resolving and downloading them."""

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
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
from streamrip.console import console
from streamrip.exceptions import NonStreamableError, TrackDownloadFailedError
from streamrip.media import album as album_module
from streamrip.media import artwork
from streamrip.media import playlist as playlist_module
from streamrip.media.album import Album, PendingAlbum
from streamrip.media.artist import Artist, Label, announce
from streamrip.media.csv_playlist import PendingCsvPlaylist, read_tracks
from streamrip.media.media import filter_prefer_explicit, resolve_or_none
from streamrip.media.playlist import (
    PendingLastfmPlaylist,
    PendingPlaylist,
    PendingPlaylistTrack,
    Playlist,
    _playlist_folder,
    best_result,
)
from streamrip.media.track import PendingSingle, PendingTrack, Track, album_folder
from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    PlaylistMetadata,
    Summary,
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


def _database():
    database = MagicMock()
    database.downloaded.return_value = False
    return database


async def test_a_cancelled_download_leaves_no_part_file(tmp_path):
    # Ctrl-C: a "Song.part.flac" beside the tracks has an audio extension, so
    # whatever indexes the library picks it up, and no later run continues it.
    class CutOff(FakeDownloadable):
        async def _download(self, path, callback):
            path.write_bytes(b"fLaC and then half a track")
            raise asyncio.CancelledError

    track = _make_track(tmp_path, "flac")
    track.downloadable = CutOff("flac")
    await track.preprocess()
    with pytest.raises(asyncio.CancelledError):
        await track.download()
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == []


@pytest.mark.parametrize(
    ("existing", "album", "downloaded", "convert", "left"),
    [
        ("flac", "Test Album", "m4a", False, ["Song.flac"]),  # lossy one not fetched
        ("m4a", "Test Album", "flac", False, ["Song.flac"]),  # lossy one removed
        ("m4a", "Test Album", "flac", True, ["Song.flac"]),  # also after converting
        # ALAC is lossless but shares the .m4a extension with AAC.
        ("alac", "Test Album", "flac", False, ["Song.flac", "Song.m4a"]),
        # Same filename stem, another album: not the same track.
        ("flac", "Some Single", "m4a", False, ["Song.flac", "Song.m4a"]),
    ],
)
async def test_of_two_copies_of_a_track_only_the_lossless_one_is_kept(
    tmp_path, monkeypatch, existing, album, downloaded, convert, left
):
    # An earlier download: real audio, tagged as streamrip does.
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
    database = _database()
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


@pytest.mark.parametrize("discnumber", ["1/../../../escaped", "2", 0])
async def test_an_invalid_disc_number_fails_the_track_before_any_file_is_made(
    pending_disc_track, tmp_path, discnumber
):
    pending, resp, _ = pending_disc_track
    resp["media_number"] = discnumber

    assert await pending.resolve() is None

    pending.db.set_failed.assert_called_once_with("qobuz", "track", pending.id)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("discnumber", [2, None])
async def test_a_multi_disc_track_downloads_into_its_disc_folder(
    pending_disc_track, discnumber
):
    pending, resp, _ = pending_disc_track
    if discnumber is None:
        del resp["media_number"]  # A missing Qobuz disc number defaults to 1.
    else:
        resp["media_number"] = discnumber

    track = await pending.resolve()
    expected = album_folder(pending.config, "qobuz", track.meta.album) / (
        f"Disc {discnumber or 1}"
    )
    await track.preprocess()
    await track.download()

    assert track.download_path.parent == expected
    assert track.download_path.read_bytes() == Path(FIXTURES["flac"]).read_bytes()


@pytest.mark.parametrize("pending_disc_track", ["single"], indirect=True)
async def test_a_single_without_a_disc_folder_when_album_folders_are_off(
    pending_disc_track,
):
    pending, _, _ = pending_disc_track
    pending.config.session.downloads.create_album_folder = False

    track = await pending.resolve()

    assert track.folder == Path(pending.config.session.downloads.folder)


@pytest.mark.parametrize("pending_disc_track", ["single"], indirect=True)
@pytest.mark.parametrize("failure", ["not streamable", "disk full"])
async def test_a_track_that_fails_while_resolving_is_kept_for_repair(
    pending_disc_track, failure
):
    pending, _, artwork = pending_disc_track
    if failure == "not streamable":
        pending.client.get_downloadable.side_effect = NonStreamableError("geoblocked")
    else:
        artwork.side_effect = OSError("disk full")

    assert await resolve_or_none(pending) is None
    pending.db.set_failed.assert_called_once_with("qobuz", "track", pending.id)


@pytest.mark.parametrize("phase", ["preprocess", "postprocess"])
async def test_a_failure_while_processing_is_stored_for_repair(
    tmp_path, monkeypatch, phase
):
    track = _make_track(tmp_path, "flac")
    track.db = db.Database(db.Dummy(), db.Failed(str(tmp_path / "failed.db")))
    monkeypatch.setattr(
        Track, phase, AsyncMock(side_effect=OSError("processing failed"))
    )

    with pytest.raises(OSError, match="processing failed"):
        await track.rip()

    assert (track.db.failed_now, track.db.downloaded_now) == (1, 0)
    assert track.db.failed.all() == [("test", "track", "123")]


async def test_a_failed_download_is_counted_once(tmp_path, monkeypatch):
    track = _make_track(tmp_path, "flac")
    track.db = db.Database(db.Dummy(), db.Failed(str(tmp_path / "failed.db")))

    async def download(self, path, callback):
        path.write_bytes(b"partial")
        raise OSError("download failed")

    monkeypatch.setattr(FakeDownloadable, "_download", download)
    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", AsyncMock())

    with pytest.raises(TrackDownloadFailedError):
        await track.rip()

    assert (track.db.failed_now, track.db.downloaded_now) == (1, 0)
    assert track.db.failed.all() == [("test", "track", "123")]
    assert not list(tmp_path.glob("*.flac"))


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


@pytest.mark.parametrize("name", ["DJ [/]", "Album [live] [bold]"])
async def test_names_from_a_service_are_printed_as_they_are(name, monkeypatch):
    # Rich reads "[...]" as markup. A "[/b]" with nothing to close raised, which
    # cost the album, the playlist or the whole discography, and "[live]" was
    # taken for a style and left out.
    monkeypatch.setattr(album_module, "rip_tracks", AsyncMock())
    monkeypatch.setattr(playlist_module, "rip_tracks", AsyncMock())
    monkeypatch.setattr(console, "_width", 200)  # one line each, not wrapped
    album = _album([MagicMock()])
    album.meta.album = album.meta.albumartist = name
    playlist = Playlist(name, MagicMock(), MagicMock(), [MagicMock()] * 30)
    with console.capture() as shown:
        announce(name, [album])
        await album.download()
        await playlist.download()
    # The artist; the album's artist and its title; the playlist.
    assert shown.get().count(name) == 4


async def test_a_downloading_track_does_not_hold_a_resolve_slot():
    n = 30
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


def _pending_album(monkeypatch, tmp_path, tracklist, downloaded):
    """A Tidal album "Encore" of `tracklist`, with the `downloaded` ones done."""
    meta = _album_meta("Encore")
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
    "downloaded",
    [set(), {"1"}, {"1", "2", "3"}],
    ids=["new", "partly downloaded", "finished"],
)
async def test_an_album_skips_the_tracks_it_already_has(
    monkeypatch, tmp_path, downloaded
):
    pending, _ = _pending_album(monkeypatch, tmp_path, ["1", "2", "3"], downloaded)

    album = await pending.resolve()

    assert [t.id for t in album.tracks] == sorted({"1", "2", "3"} - downloaded)
    # An artist resolves all its albums before its filters drop some: nothing
    # is counted or made for an album until it is wanted.
    assert pending.db.skipped_now == 0
    assert not (tmp_path / "Encore").exists()

    await album.preprocess()

    assert pending.db.skipped_now == len(downloaded)
    # A finished album gets no folder.
    assert (tmp_path / "Encore").is_dir() is (len(downloaded) < 3)


class _BookletDownloadable:
    downloaded: list[str] = []

    def __init__(self, _session, url, _extension):
        self.url = url

    async def download(self, path, _callback):
        if "broken" in self.url:
            path.write_bytes(b"%PD")  # cut off mid-download
            raise ConnectionError("gone")
        if "cancelled" in self.url:
            path.write_bytes(b"%PD")
            raise asyncio.CancelledError
        type(self).downloaded.append(self.url)
        path.write_bytes(b"%PDF")


async def test_booklets_are_saved_as_pdfs(monkeypatch, tmp_path):
    monkeypatch.setattr(album_module, "BasicDownloadable", _BookletDownloadable)
    _BookletDownloadable.downloaded = []
    (tmp_path / "Liner Notes.pdf").write_bytes(b"%PDF")
    booklets = [
        {"description": "Digital Booklet", "url": "https://q/1.pdf"},
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


async def test_a_booklet_cut_off_by_a_cancel_is_not_kept(monkeypatch, tmp_path):
    # At its final name the next run took the half PDF for the booklet.
    monkeypatch.setattr(album_module, "BasicDownloadable", _BookletDownloadable)
    booklets = [{"description": "Booklet", "url": "https://q/cancelled.pdf"}]
    with pytest.raises(asyncio.CancelledError):
        await album_module.download_booklets(None, booklets, tmp_path)
    assert list(tmp_path.iterdir()) == []


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


@pytest.mark.parametrize(
    ("run", "fails_in"),
    [
        (lambda albums: _artist(albums)._download_async(NO_FILTERS), "resolve"),
        (lambda albums: _artist(albums)._resolve_then_download(NO_FILTERS), "resolve"),
        (lambda albums: Label("label", albums, None, None).download(), "rip"),
    ],
    ids=["artist", "artist, repeats on", "label"],
)
async def test_a_failing_album_does_not_stop_the_others(run, fails_in):
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


@pytest.mark.parametrize(
    ("albums", "kept"),
    [
        # Of two editions the higher bit depth wins, then the higher sampling
        # rate, then the explicit one.
        (
            [
                _release("a1", "Test Album"),
                _release("a2", "Test Album (Deluxe)", 44.1, 24),
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
        # Another album is not an edition.
        ([_release("a1", "Album A"), _release("b1", "Album B", 96, 24)], ["a1", "b1"]),
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
        ("extras", {"albumartist": "Various Artists"}, False),
        # An album the artist shares is not a feature.
        (
            "features",
            {"albumartist": "artist, Other", "albumartists": ["artist", "Other"]},
            True,
        ),
        ("features", {"albumartist": "Someone Else"}, False),
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


@pytest.mark.parametrize("selected", [False, True])
async def test_artist_counts_skips_only_for_selected_albums(
    monkeypatch, tmp_path, selected
):
    pending, _ = _pending_album(monkeypatch, tmp_path, ["1", "2"], {"1", "2"})
    pending.config.session.cli.progress_bars = False
    filters = pending.config.session.artist_filters
    filters.repeats = False
    monkeypatch.setattr(Artist, "_wanted", lambda self, album, filters: selected)
    artist = Artist("Artist", [pending], pending.client, pending.config)

    await artist._download_async(filters)

    assert pending.db.skipped_now == (2 if selected else 0)


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


def _lastfm_playlist(monkeypatch, total_tracks):
    session = AsyncMock()
    session.__aenter__.return_value = session

    @asynccontextmanager
    async def get(url, **kwargs):
        start = (kwargs.get("params", {}).get("page", 1) - 1) * 50
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

    session.get = MagicMock(side_effect=get)
    monkeypatch.setattr("streamrip.media.playlist.new_session", lambda **kw: session)
    return PendingLastfmPlaylist(
        "https://www.last.fm/playlist/test",
        MagicMock(),
        [],
        Config.defaults(),
        MagicMock(),
    )


@pytest.mark.parametrize("count", [1, 51, 120])
async def test_a_lastfm_playlist_is_read_page_by_page(monkeypatch, count):
    playlist = _lastfm_playlist(monkeypatch, count)

    title, tracks = await playlist._parse_lastfm_playlist(playlist.lastfm_url)

    assert title == "Mix & Match"
    assert tracks == [(f"Song {i} & Co", "Artist & Co") for i in range(count)]


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
    database = _database()
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


async def test_a_playlist_track_is_tagged_as_part_of_one_album(monkeypatch):
    monkeypatch.setattr(
        "streamrip.media.playlist.download_artwork",
        AsyncMock(return_value=(None, None)),
    )
    client = MagicMock()
    client.source = "deezer"
    client.get_metadata = AsyncMock(return_value=_deezer_track(position=5, disc=2))
    client.get_downloadable = AsyncMock()
    client.before_track = AsyncMock()
    pending = PendingPlaylistTrack(
        "7", client, Config.defaults(), "/x", "Road Trip", 3, _database(), total=50
    )

    meta = (await pending.resolve()).meta

    assert (meta.tracknumber, meta.discnumber) == (3, 1)
    assert (meta.album.tracktotal, meta.album.disctotal) == (50, 1)
    # One album artist for the whole playlist, or servers split it per artist.
    assert (meta.album.album, meta.album.albumartist) == (
        "Road Trip",
        "Various Artists",
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
def test_a_playlist_folder_is_one_folder(restrict, folder):
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.filepaths.restrict_characters = restrict
    assert _playlist_folder(config, "Café / Mix") == Path(folder)


@pytest.mark.parametrize("name", ["", ".."])
def test_a_playlist_name_that_is_no_folder_is_refused(tmp_path, name):
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path)
    with pytest.raises(ValueError, match="playlist folder"):
        _playlist_folder(config, name)


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
    config = Config.defaults()
    config.session.metadata.set_playlist_to_album = False
    return PendingPlaylistTrack(requested, client, config, "/x", "Mix", 1, _database())


@pytest.fixture
def artwork_fetched(monkeypatch):
    artwork = AsyncMock(return_value=(None, None))
    monkeypatch.setattr("streamrip.media.playlist.download_artwork", artwork)
    return artwork


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
    # A geoblocked or delisted Deezer track is served from FALLBACK.SNG_ID's
    # release. Its own metadata no longer describes the file, and its cover is
    # Deezer's grey placeholder (3 of 9 such tracks in a real 60-track playlist).
    track = await _served_from("7", served).resolve()

    assert (track.meta.title, track.meta.album.album) == (title, f"{title} album")
    # One cover, the served release's: the placeholder is not even fetched.
    covers = [c.args[2].get_size("large")[1] for c in artwork_fetched.await_args_list]
    assert covers == [cover]


async def test_a_fallback_track_is_recorded_under_the_requested_id(
    artwork_fetched, monkeypatch
):
    # Re-running the playlist must skip it.
    monkeypatch.setattr("streamrip.media.track.tag_file", AsyncMock())
    pending = _served_from("7", "99")

    track = await pending.resolve()
    await track.postprocess()

    assert track.meta.title == "Served"  # tags still come from the served track
    pending.db.set_downloaded.assert_called_once_with("deezer", "7")


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
        # A comma in a name does not make the commas the separator.
        (
            "Earth, Wind & Fire;September\nDaft Punk;Get Lucky\n",
            [
                ("September", ["Earth, Wind & Fire"], "", None),
                ("Get Lucky", ["Daft Punk"], "", None),
            ],
        ),
        # Excel's byte order mark does not hide the header.
        ("﻿Title;Artist\nSong;Band\n", [("Song", ["Band"], "", None)]),
        (
            "title,artists\n\n,Nobody\nSong,Somebody\n , \n",
            [("Song", ["Somebody"], "", None)],
        ),
    ],
    ids=[
        "the csv of Music-Sync",
        "exportify",
        "tunemymusic",
        "semicolons",
        "no header",
        "mac",
        "comma in a name",
        "byte order mark",
        "blank rows and rows without a title",
    ],
)
def test_a_csv_list_is_read(tmp_path, text, expected):
    assert _rows(read_tracks(_write(tmp_path, text))) == expected


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        ("duration_ms\n123456.0\n", 123456),
        ("duration\n3:45\n", 225_000),
        # A bare number could be seconds or milliseconds: better to say nothing.
        ("duration\n225\n", None),
    ],
)
def test_a_length_is_only_taken_when_its_unit_is_clear(tmp_path, duration, expected):
    header, value = duration.split("\n")[:2]
    path = _write(tmp_path, f"title,{header}\nSong,{value}\n")

    assert read_tracks(path)[0].duration_ms == expected


def test_a_track_number_column_does_not_replace_the_title(tmp_path):
    path = _write(tmp_path, "Title,Artist,Album,Track\nHeroes,David Bowie,Heroes,3\n")
    assert read_tracks(path)[0].title == "Heroes"


def test_a_file_that_is_not_utf8_is_refused_with_advice(tmp_path):
    path = _write(tmp_path, "title,artists\nCafé,Band\n", encoding="latin-1")

    with pytest.raises(ValueError, match="UTF-8"):
        read_tracks(path)


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
        ([_summary("Get Lucky", "Somebody Else")], None),
        ([_summary("Get Lucky (Karaoke Version)", "Daft Punk")], None),
    ],
    ids=["the track", "the edit of its length", "another artist", "another version"],
)
def test_the_search_result_that_is_the_track_is_chosen(results, chosen):
    hit = best_result(WANTED, results)
    assert (hit[0].id if hit else None) == chosen


class Catalogue:
    """A source that finds tracks by a table of query -> results."""

    def __init__(self, source, table, fail=()):
        self.source = source
        self.table = table
        self.fail = fail

    async def search(self, media_type, query, limit=50, offset=0):
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


def _csv_playlist(tmp_path, tracks, client, fallback=None):
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path)
    config.session.cli.progress_bars = False
    fallbacks = [fallback] if fallback is not None else []
    return PendingCsvPlaylist("Mix", tracks, client, fallbacks, config, MagicMock())


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

    assert playlist.name == "Mix"
    assert [(t.id, t.position, t.total) for t in playlist.tracks] == [
        ("11", 1, 3),
        ("33", 3, 3),  # the position in the list, also where one is missing
    ]
    assert {t.folder for t in playlist.tracks} == {tmp_path / "Mix"}


@pytest.mark.parametrize(
    ("tracks", "tidal", "fallback", "found"),
    [
        (
            [ONE, TWO],
            Catalogue("tidal", HAS_ONE),
            Catalogue("spotify", HAS_TWO),
            [("11", 1, "tidal"), ("22", 2, "spotify")],
        ),
        (
            [ONE, TWO],
            Catalogue("tidal", HAS_ONE),
            None,
            [("11", 1, "tidal")],
        ),
        (
            [ONE, TWO],
            Catalogue("tidal", HAS_TWO, fail={"Band Song One"}),
            None,
            [("22", 2, "tidal")],
        ),
        # A "Song One (Live)" is not "Song One".
        (
            [ONE],
            Catalogue("tidal", {"Band Song One": [_item(99, "Song One (Live)")]}),
            None,
            None,
        ),
        ([ONE, TWO], Catalogue("tidal", {}), None, None),
    ],
    ids=[
        "the fallback has what the source lacks",
        "a track that is nowhere",
        "a search that fails",
        "a wrong result",
        "nothing found",
    ],
)
async def test_the_tracks_of_a_list_are_found_where_they_are(
    tmp_path, tracks, tidal, fallback, found
):
    playlist = await _csv_playlist(tmp_path, tracks, tidal, fallback).resolve()

    if found is None:
        assert playlist is None
    else:
        assert [(t.id, t.position, t.client.source) for t in playlist.tracks] == found


async def test_the_full_title_is_tried_when_the_short_one_finds_nothing(tmp_path):
    wanted = MatchTrack("Song (feat. Guest)", ["Band"], duration_ms=200_000)
    tidal = Catalogue(
        "tidal", {"Band Song (feat. Guest)": [_item(5, "Song (feat. Guest)")]}
    )

    playlist = await _csv_playlist(tmp_path, [wanted], tidal).resolve()

    assert [t.id for t in playlist.tracks] == ["5"]


@pytest.mark.parametrize(
    ("failures", "saved", "tries"),
    [
        ([requests.ConnectionError("interrupted download")], True, 2),
        ([requests.HTTPError(response=MagicMock(status_code=404))], False, 1),
        (
            [requests.ConnectionError("down")] * artwork.ARTWORK_ATTEMPTS,
            False,
            artwork.ARTWORK_ATTEMPTS,
        ),
    ],
    ids=["interrupted: retried", "missing: not retried", "keeps failing"],
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
