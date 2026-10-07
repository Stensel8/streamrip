"""Unit tests for Deezer client behaviour that needs no network or ARL."""

import logging
from unittest.mock import AsyncMock, Mock

import pytest
from deezer.errors import GWAPIError

from streamrip.client.deezer import DeezerClient
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError

TRACK_INFO = {
    "FILESIZE_FLAC": 30_000_000,
    "FILESIZE_MP3_320": 9_000_000,
    "FILESIZE_MP3_128": 4_000_000,
    "TRACK_TOKEN": "token",
}


def _client(lower_quality=True, **account) -> DeezerClient:
    config = Config.defaults()
    config.session.deezer.lower_quality_if_not_available = lower_quality
    client = DeezerClient(config)
    client.client = Mock()
    client.client.current_user = {"license_token": "x", **account}
    client.client.gw.get_track.return_value = dict(TRACK_INFO)
    client.client.get_track_url.return_value = "https://cdn.example/track"
    client.session = Mock()
    return client


async def test_free_account_is_clamped_to_mp3_128():
    """Upstream #1015: quality 1 on a free account used to claim HiFi was needed."""
    client = _client(can_stream_hq=False, can_stream_lossless=False)
    dl = await client.get_downloadable("1", quality=1)
    assert dl.quality == 0
    client.client.get_track_url.assert_called_once_with("token", "MP3_128")


async def test_premium_account_is_clamped_to_mp3_320():
    client = _client(can_stream_hq=True, can_stream_lossless=False)
    dl = await client.get_downloadable("1", quality=2)
    assert dl.quality == 1 and dl.extension == "mp3"


async def test_clamp_can_be_disabled():
    client = _client(lower_quality=False, can_stream_hq=False)
    with pytest.raises(NonStreamableError, match="subscription"):
        await client.get_downloadable("1", quality=1)


async def test_out_of_range_quality_does_not_crash():
    """`streamrip --quality 4` used to raise IndexError for Deezer."""
    client = _client(can_stream_hq=True, can_stream_lossless=True)
    dl = await client.get_downloadable("1", quality=4)
    assert dl.quality == 2 and dl.extension == "flac"


async def test_playlist_falls_back_to_gw_api():
    client = _client()
    client.client.api.get_playlist.side_effect = Exception("PermissionException")
    client.client.gw.get_playlist_page.return_value = {"DATA": {"TITLE": "Mine"}}
    client.client.gw.get_playlist_tracks.return_value = [
        {"SNG_ID": "11"},
        {"SNG_ID": "12"},
    ]
    resp = await client.get_playlist("42")
    assert resp["title"] == "Mine"
    assert [t["id"] for t in resp["tracks"]] == ["11", "12"]


async def test_redirected_album_id_is_followed():
    """Upstream #893: /album/<old id>/tracks has no data, the new id does."""
    client = _client()
    client.client.api.get_album.return_value = {"id": 723513301, "title": "New"}

    def tracks(album_id):
        if str(album_id) == "997825":
            raise Exception("DataException: no data")
        return {"data": [{"id": 1, "disk_number": 1}]}

    client.client.api.get_album_tracks.side_effect = tracks
    resp = await client.get_album("997825")
    assert resp["track_total"] == 1


def test_synced_lyrics_are_formatted_as_lrc():
    lrc = DeezerClient._format_synced_lyrics(
        [
            {"lrc_timestamp": "[00:01.00]", "line": "Hello"},
            {"line": ""},
            {"lrc_timestamp": "[00:05.50]", "line": "World"},
        ]
    )
    assert lrc == "[00:01.00]Hello\n\n[00:05.50]World"


def _client_with_a_track(**gw) -> DeezerClient:
    client = _client()
    client.client.api.get_track.return_value = {"id": 1, "album": {"id": 5}}
    client.get_album = AsyncMock(return_value={"id": 5})
    client.client.gw.get_track_lyrics.configure_mock(**gw)
    return client


async def test_an_error_answer_to_the_lyrics_request_means_no_lyrics(caplog):
    """Deezer says it has none with an error: no warning for every track."""
    client = _client_with_a_track(side_effect=GWAPIError('{"DATA_ERROR": "none"}'))

    with caplog.at_level(logging.DEBUG, logger="streamrip"):
        track = await client.get_track("1")

    assert "lyrics" not in track
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_lyrics_request_that_breaks_is_still_a_warning(caplog):
    client = _client_with_a_track(side_effect=ConnectionError("down"))

    with caplog.at_level(logging.DEBUG, logger="streamrip"):
        track = await client.get_track("1")

    assert "lyrics" not in track
    assert "Failed to get lyrics for 1: down" in caplog.text


async def test_another_error_answer_to_the_lyrics_request_is_still_a_warning(caplog):
    """Only "no data" means no lyrics: an error answer is not always that."""
    client = _client_with_a_track(
        side_effect=GWAPIError('{"GATEWAY_ERROR": "too many requests"}')
    )

    with caplog.at_level(logging.DEBUG, logger="streamrip"):
        track = await client.get_track("1")

    assert "lyrics" not in track
    assert "Failed to get lyrics for 1:" in caplog.text
    assert "too many requests" in caplog.text


async def test_lyrics_deezer_does_send_are_still_used():
    client = _client_with_a_track(return_value={"LYRICS_TEXT": "la la la"})

    track = await client.get_track("1")

    assert track["lyrics"] == "la la la"
