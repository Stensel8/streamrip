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
