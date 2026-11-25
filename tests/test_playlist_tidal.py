"""Tidal's official API (JSON:API) for the playlist tools."""

from itertools import starmap
from typing import Any

import pytest
import responses
from playlist_support import ISRC_A, ISRC_B, sent_json, sent_query

from streamrip.sync.http import ApiClient
from streamrip.sync.models import Track
from streamrip.sync.providers.tidal import (
    API,
    TidalProvider,
    parse_duration,
    parse_tracks,
)


@pytest.fixture
def tidal() -> TidalProvider:
    return TidalProvider(
        ApiClient(API, lambda force: "tok", sleep=lambda _: None), country="NL"
    )


def tidal_track(
    track_id: str,
    title: str = "Get Lucky",
    version: str | None = None,
    isrc: str = ISRC_A,
) -> dict[str, Any]:
    """A track resource as the TIDAL API sends it: artist and album are references into ``included``."""
    attributes = {"title": title, "isrc": isrc, "duration": "PT4M8S"}
    if version:
        attributes["version"] = version
    return {
        "id": track_id,
        "type": "tracks",
        "attributes": attributes,
        "relationships": {
            "artists": {"data": [{"id": "9", "type": "artists"}]},
            "albums": {"data": [{"id": "5", "type": "albums"}]},
        },
    }


def refs(*ids: str, kind: str = "tracks") -> dict[str, Any]:
    return {"data": [{"id": i, "type": kind} for i in ids]}


INCLUDED = [
    {"id": "9", "type": "artists", "attributes": {"name": "Daft Punk"}},
    {"id": "5", "type": "albums", "attributes": {"title": "Random Access Memories"}},
]
COLLECTION = f"{API}/userCollectionTracks/me/relationships/items"


@pytest.mark.parametrize(
    ("value", "milliseconds"),
    [("PT4M8S", 248000), ("PT1H2M3.5S", 3723500), ("nonsense", None), (None, None)],
)
def test_parse_duration(value, milliseconds):
    assert parse_duration(value) == milliseconds


def test_parse_tracks_resolves_artist_album_and_version():
    (parsed,) = parse_tracks(
        {"data": [tidal_track("111", version="Radio Edit")], "included": INCLUDED}
    )
    assert parsed == Track(
        "Get Lucky (Radio Edit)",
        ["Daft Punk"],
        "Random Access Memories",
        248000,
        isrc=ISRC_A,
        ids={"tidal": "111"},
    )


def test_parse_tracks_ignores_videos():
    video = {"id": "1", "type": "videos", "attributes": {"title": "Clip"}}
    assert parse_tracks({"data": [video]}) == []


@responses.activate
def test_liked_tracks_keep_their_order_and_follow_the_cursor(tidal):
    next_page = "/userCollectionTracks/me/relationships/items?page%5Bcursor%5D=CUR"
    responses.get(COLLECTION, json={**refs("2", "1"), "links": {"next": next_page}})
    responses.get(COLLECTION, json=refs("3"))
    responses.get(
        f"{API}/tracks",
        json={
            "data": list(
                starmap(tidal_track, [("1", "One"), ("2", "Two"), ("3", "Three")])
            ),
            "included": INCLUDED,
        },
    )
    assert [t.title for t in tidal.liked_tracks()] == ["Two", "One", "Three"]
    cursors = [sent_query(call).get("page[cursor]") for call in responses.calls]
    assert ["CUR"] in cursors


@responses.activate
def test_playlists_are_the_ones_you_own(tidal):
    playlist = {
        "id": "uuid-1",
        "type": "playlists",
        "attributes": {"name": "Road trip", "numberOfItems": 12},
    }
    responses.get(f"{API}/playlists", json={"data": [playlist]})
    (found,) = tidal.playlists()
    assert (found.id, found.name, found.track_count) == ("uuid-1", "Road trip", 12)
    assert sent_query(responses.calls[0])["filter[owners.id]"] == ["me"]


@responses.activate
def test_isrcs_are_looked_up_twenty_at_a_time_and_grouped_by_code(tidal):
    codes = [f"USAAA{n:07d}" for n in range(45)]
    found_tracks = [
        tidal_track("111", isrc=codes[0]),
        tidal_track("222", isrc=codes[1]),
    ]
    responses.get(f"{API}/tracks", json={"data": found_tracks, "included": INCLUDED})
    found = tidal.lookup_isrcs(codes)
    batches = [len(sent_query(call)["filter[isrc]"]) for call in responses.calls]
    assert batches == [20, 20, 5]
    assert sorted(found) == [codes[0], codes[1]]
    assert found[codes[0]][0].ids == {"tidal": "111"}


@responses.activate
def test_an_isrc_lookup_reads_every_page(tidal):
    # One ISRC is often on a single, an album and a compilation: more than one page.
    responses.get(
        f"{API}/tracks",
        json={
            "data": [tidal_track("111", isrc=ISRC_A)],
            "included": INCLUDED,
            "links": {"next": "/tracks?page%5Bcursor%5D=P2"},
        },
    )
    responses.get(
        f"{API}/tracks",
        json={"data": [tidal_track("222", isrc=ISRC_B)], "included": INCLUDED},
    )
    assert sorted(tidal.lookup_isrcs([ISRC_A, ISRC_B])) == [ISRC_A, ISRC_B]
    assert sent_query(responses.calls[1])["page[cursor]"] == ["P2"]


@responses.activate
def test_find_finds_a_track_by_isrc(tidal):
    responses.get(
        f"{API}/tracks",
        json={"data": [tidal_track("111", isrc=ISRC_B)], "included": INCLUDED},
    )
    match = tidal.find(Track("Whatever", ["Someone"], isrc=ISRC_B))
    assert match is not None
    assert (match.method, match.track.ids) == ("isrc", {"tidal": "111"})


@responses.activate
def test_search_keeps_tidals_relevance_order(tidal):
    result = {
        "id": "s",
        "type": "searchResults",
        "relationships": {"tracks": refs("2", "1")},
    }
    responses.get(f"{API}/searchResults", json={"data": [result]})
    responses.get(
        f"{API}/tracks",
        json={
            "data": [tidal_track("1", "One"), tidal_track("2", "Two")],
            "included": INCLUDED,
        },
    )
    assert [t.title for t in tidal.search("daft punk")] == ["Two", "One"]
    assert sent_query(responses.calls[0])["filter[query]"] == ["daft punk"]


@responses.activate
def test_create_playlist_posts_a_json_api_body(tidal):
    responses.post(
        f"{API}/playlists",
        json={"data": {"id": "uuid-9", "type": "playlists"}},
        status=201,
    )
    assert tidal.create_playlist("Mix", "About") == "uuid-9"
    assert sent_json(responses.calls[0]) == {
        "data": {
            "type": "playlists",
            "attributes": {"name": "Mix", "description": "About"},
        }
    }
    assert responses.calls[0].request.headers["Content-Type"] == (
        "application/vnd.api+json"
    )


@responses.activate
def test_add_to_playlist_skips_duplicates_server_side(tidal):
    responses.post(f"{API}/playlists/uuid-9/relationships/items", status=201)
    tracks = [Track("T0", ids={"tidal": "0"}), Track("T1", ids={"tidal": "1"})]
    tidal.add_to_playlist("uuid-9", [*tracks, Track("No id")])
    assert sent_json(responses.calls[0]) == {
        **refs("0", "1"),
        "meta": {"onDuplicates": "SKIP"},
    }


@responses.activate
def test_add_favorite_tracks_posts_to_the_collection(tidal):
    responses.post(COLLECTION, status=201)
    tidal.add_favorite_tracks([Track("T", ids={"tidal": "7"}), Track("No id")])
    assert sent_json(responses.calls[0]) == refs("7")


@responses.activate
def test_albums_are_searched_and_added_like_tracks(tidal):
    result = {"id": "s", "type": "searchResults"}
    result["relationships"] = {"albums": refs("5", kind="albums")}
    responses.get(f"{API}/searchResults", json={"data": [result]})
    album = {
        "id": "5",
        "type": "albums",
        "attributes": {"title": "Discovery"},
        "relationships": {"artists": refs("9", kind="artists")},
    }
    responses.get(f"{API}/albums", json={"data": [album], "included": INCLUDED})
    (found,) = tidal.search_albums("Discovery Daft Punk")
    assert (found.title, found.artists, found.ids) == (
        "Discovery",
        ["Daft Punk"],
        {"tidal": "5"},
    )

    responses.post(f"{API}/userCollectionAlbums/me/relationships/items", status=201)
    tidal.add_favorite_albums([found])
    assert sent_json(responses.calls[2]) == refs("5", kind="albums")
