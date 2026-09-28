from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.exceptions import NonStreamableError
from streamrip.media.playlist import PendingLastfmPlaylist, PendingPlaylistTrack


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
    from streamrip.config import Config

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
