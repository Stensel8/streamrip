"""Lyrics are optional: failing to fetch them must never cost the track.

Issue #959: a 401 from the lyrics endpoint escaped get_metadata() and every
track in the album was reported as a download error, with only cover.jpg
written. A 404 was already contained; any other request failure was not.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from streamrip.client.tidal import TidalClient
from streamrip.exceptions import ItemNotFoundError

TRACK = {"id": 1, "title": "t", "album": {"id": 2}}


def _client(*, lyrics_error=None, track_error=None):
    """A Tidal client whose lyrics or track request fails as asked."""
    client = TidalClient.__new__(TidalClient)
    client.global_config = MagicMock()
    client.global_config.session.conversion.enabled = False
    client.config = MagicMock()
    client._albums = {}
    client._album_requests = {}

    async def request(path, params=None, base=None):
        if path.endswith("/lyrics"):
            if lyrics_error:
                raise lyrics_error
            return {"lyrics": "la la"}
        if track_error:
            raise track_error
        return dict(TRACK)

    client._api_request = AsyncMock(side_effect=request)
    return client


def _http_error(status):
    return aiohttp.ClientResponseError(
        MagicMock(real_url="https://tidal.com/v1/tracks/1/lyrics"),
        (),
        status=status,
        message="Unauthorized",
    )


@pytest.mark.asyncio
async def test_lyrics_401_does_not_abort_track(caplog):
    item = await _client(lyrics_error=_http_error(401)).get_metadata("1", "track")
    assert item["title"] == "t"
    assert "Failed to get lyrics" in caplog.text


@pytest.mark.asyncio
async def test_lyrics_timeout_does_not_abort_track():
    item = await _client(lyrics_error=asyncio.TimeoutError()).get_metadata("1", "track")
    assert item["title"] == "t"


@pytest.mark.asyncio
async def test_no_lyrics_is_still_quiet(caplog):
    item = await _client(lyrics_error=ItemNotFoundError("x")).get_metadata("1", "track")
    assert item["title"] == "t"
    assert "Failed to get lyrics" not in caplog.text


@pytest.mark.asyncio
async def test_track_request_failure_still_raises():
    """Only the optional lyrics call is contained -- real failures still surface."""
    with pytest.raises(aiohttp.ClientResponseError):
        await _client(track_error=_http_error(401)).get_metadata("1", "track")
