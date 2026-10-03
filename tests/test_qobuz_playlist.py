"""Qobuz playlist parsing and pagination.

Qobuz caps the inline track list of playlist/get at one page (500 tracks) and,
since its July 2026 API change, often returns it empty. The complete list of
ids comes back in the ``track_ids`` extra instead.
"""

import json
from unittest.mock import AsyncMock

import pytest

from streamrip.client.qobuz import QobuzClient
from streamrip.metadata import PlaylistMetadata

with open("tests/qobuz_track_resp.json") as f:
    TRACK = json.load(f)


def test_track_ids_preferred_over_truncated_inline_list():
    """When the inline track list is truncated, the full list of track ids is used."""
    resp = {
        "name": "Long playlist",
        "tracks": {"items": [TRACK], "total": 3},
        "track_ids": [1, 2, 3],
    }
    meta = PlaylistMetadata.from_resp(resp, "qobuz")
    assert meta.ids == ["1", "2", "3"]


def test_empty_inline_list_falls_back_to_track_ids():
    """An empty inline list falls back to the track ids."""
    resp = {"name": "New API shape", "tracks": {"items": []}, "track_ids": [7, 8]}
    assert PlaylistMetadata.from_resp(resp, "qobuz").ids == ["7", "8"]


def test_inline_list_used_when_complete():
    """A complete inline list is used as it is."""
    resp = {"name": "Short", "tracks": {"items": [TRACK], "total": 1}}
    meta = PlaylistMetadata.from_resp(resp, "qobuz")
    assert meta.ids == [str(TRACK["id"])]


def test_malformed_track_does_not_sink_playlist():
    """One malformed track doesn't cost the playlist."""
    resp = {"name": "Mixed", "tracks": {"items": [{"id": 1}, TRACK]}}
    meta = PlaylistMetadata.from_resp(resp, "qobuz")
    assert meta.ids == [str(TRACK["id"])]


@pytest.mark.asyncio
async def test_long_playlist_without_track_ids_is_paginated():
    c = QobuzClient.__new__(QobuzClient)
    c._request_ok = AsyncMock(
        side_effect=[
            {"tracks": {"items": [{"id": i} for i in range(500, 1000)]}},
            {"tracks": {"items": [{"id": i} for i in range(1000, 1200)]}},
        ]
    )
    resp = {
        "tracks": {"items": [{"id": i} for i in range(500)], "total": 1200},
    }
    await c._fetch_remaining_playlist_tracks(
        "playlist/get", {"playlist_id": "x", "limit": 500, "offset": 0}, resp
    )
    assert [t["id"] for t in resp["tracks"]["items"]] == list(range(1200))
    offsets = [call.args[1]["offset"] for call in c._request_ok.call_args_list]
    assert offsets == [500, 1000]


@pytest.mark.asyncio
async def test_no_pagination_when_track_ids_present():
    c = QobuzClient.__new__(QobuzClient)
    c._request_ok = AsyncMock()
    resp = {"tracks": {"items": [{"id": 1}], "total": 900}, "track_ids": [1, 2]}
    await c._fetch_remaining_playlist_tracks("playlist/get", {"limit": 500}, resp)
    c._request_ok.assert_not_called()


def test_a_playlist_item_that_is_not_a_track_is_skipped():
    """Anything in the list that isn't a track dict is passed over, not fatal."""
    resp = {"name": "Odd", "tracks": {"items": [None, "x", TRACK]}}
    meta = PlaylistMetadata.from_resp(resp, "qobuz")
    assert meta.ids == [str(TRACK["id"])]
