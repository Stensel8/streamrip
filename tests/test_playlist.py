import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.config import Config
from streamrip.exceptions import NonStreamableError
from streamrip.media.playlist import (
    LASTFM_MAX_TRACKS,
    PendingLastfmPlaylist,
    PendingPlaylistTrack,
    _playlist_folder,
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [LASTFM_MAX_TRACKS + 1, 1_000_000_000])
async def test_lastfm_rejects_excessive_counts_before_pagination(monkeypatch, count):
    playlist, session = _lastfm_playlist(monkeypatch, count)

    with pytest.raises(ValueError, match="supported limit"):
        await playlist._parse_lastfm_playlist(playlist.lastfm_url)

    session.get.assert_called_once_with(playlist.lastfm_url)
    session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_lastfm_excessive_count_stops_resolution(monkeypatch, caplog):
    playlist, session = _lastfm_playlist(monkeypatch, LASTFM_MAX_TRACKS + 1)

    assert await playlist.resolve() is None

    playlist.client.search.assert_not_called()
    session.get.assert_called_once_with(playlist.lastfm_url)
    assert "supported limit" in caplog.text


@pytest.mark.asyncio
async def test_lastfm_page_failure_stops_pagination_and_closes_session(monkeypatch):
    playlist, session = _lastfm_playlist(monkeypatch, 151, fail_page=2)

    with pytest.raises(ConnectionError, match="page unavailable"):
        await playlist._parse_lastfm_playlist(playlist.lastfm_url)

    assert session.get.call_count == 2
    session.__aexit__.assert_awaited_once()


def _search_client(source, search):
    client = MagicMock()
    client.source = source
    client.search = AsyncMock(side_effect=search)
    return client


@pytest.mark.asyncio
async def test_lastfm_search_error_falls_back_instead_of_failing():
    main = _search_client("tidal", ConnectionError("boom"))
    fallback = _search_client("deezer", [[{"data": [{"id": 7, "title": "Song"}]}]])
    playlist = PendingLastfmPlaylist("url", main, fallback, MagicMock(), MagicMock())
    status = PendingLastfmPlaylist.Status(0, 0, 1)
    callback = MagicMock()

    assert await playlist._make_query("Song Artist", status, callback) == ("7", True)
    assert (status.found, status.failed) == (1, 0)
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_lastfm_query_with_an_empty_page_counts_as_not_found():
    main = _search_client("deezer", [[{"data": []}]])
    playlist = PendingLastfmPlaylist("url", main, None, MagicMock(), MagicMock())
    status = PendingLastfmPlaylist.Status(0, 0, 1)

    assert await playlist._make_query("x", status, MagicMock()) == (None, False)
    assert status.failed == 1


def _playlist_track(get_metadata):
    client = MagicMock()
    client.source = "tidal"
    client.get_metadata = get_metadata
    db = MagicMock()
    db.downloaded.return_value = False
    track = PendingPlaylistTrack("42", client, MagicMock(), "/x", "Mix", 1, db)
    return track, db


@pytest.mark.asyncio
async def test_playlist_track_that_cannot_be_fetched_is_kept_for_repair():
    track, db = _playlist_track(AsyncMock(side_effect=NonStreamableError("gone")))

    assert await track.resolve() is None
    db.set_failed.assert_called_once_with("tidal", "track", "42")


@pytest.mark.asyncio
async def test_playlist_track_with_unreadable_metadata_is_kept_for_repair():
    track, db = _playlist_track(AsyncMock(return_value={"unexpected": "shape"}))

    assert await track.resolve() is None
    db.set_failed.assert_called_once_with("tidal", "track", "42")


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


@pytest.mark.asyncio
async def test_playlist_track_is_tagged_as_part_of_one_album(monkeypatch):
    monkeypatch.setattr(
        "streamrip.media.playlist.download_artwork",
        AsyncMock(return_value=(None, None)),
    )
    client = MagicMock()
    client.source = "deezer"
    client.get_metadata = AsyncMock(return_value=_deezer_track(position=5, disc=2))
    client.get_downloadable = AsyncMock()
    db = MagicMock()
    db.downloaded.return_value = False
    pending = PendingPlaylistTrack(
        "7", client, Config.defaults(), "/x", "Road Trip", 3, db, total=50
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


def test_tracks_uploaded_to_deezer_are_left_out_of_a_playlist():
    from streamrip.metadata import PlaylistMetadata

    resp = {"title": "Mix", "tracks": [{"id": 1}, {"id": -5}, {"id": "2"}]}
    assert PlaylistMetadata.from_deezer(resp).ids() == ["1", "2"]


def test_playlist_folder_is_one_folder_and_follows_restrict_characters():
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    assert _playlist_folder(config, "Café / Mix") == "/music/Café  Mix"
    config.session.filepaths.restrict_characters = True
    assert _playlist_folder(config, "Café / Mix") == "/music/Caf  Mix"
