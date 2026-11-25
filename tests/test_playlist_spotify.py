"""Spotify's Web API for the playlist tools: liked songs, playlists, search and changes."""

from typing import Any

import pytest
import responses
from playlist_support import ISRC_A, ISRC_B, sent_json, sent_query

from streamrip.sync.errors import ProviderError
from streamrip.sync.http import ApiClient
from streamrip.sync.models import Track
from streamrip.sync.providers.spotify import API, SpotifyProvider, parse_track


@pytest.fixture
def spotify() -> SpotifyProvider:
    return SpotifyProvider(ApiClient(API, lambda force: "tok", sleep=lambda _: None))


def sp_track(
    name: str = "Song", uri: str = "spotify:track:1", isrc: str = ISRC_A, **extra: Any
) -> dict[str, Any]:
    """A track object as the Spotify API sends it."""
    return {
        "type": "track",
        "name": name,
        "uri": uri,
        "duration_ms": 200000,
        "artists": [{"name": "Artist"}],
        "album": {"name": "Album"},
        "external_ids": {"isrc": isrc},
        **extra,
    }


def spotify_id(n: int) -> Track:
    return Track(f"T{n}", ids={"spotify": f"spotify:track:{n}"})


def test_parse_track_maps_the_fields():
    assert parse_track(sp_track()) == Track(
        "Song",
        ["Artist"],
        "Album",
        200000,
        isrc=ISRC_A,
        ids={"spotify": "spotify:track:1"},
    )


def test_podcast_episodes_and_local_files_cannot_be_added_to_a_playlist():
    assert parse_track({"type": "episode", "name": "A podcast"}) is None
    local = parse_track(sp_track(is_local=True))
    assert local is not None
    assert local.ids == {}


@responses.activate
def test_liked_tracks_follow_the_pagination(spotify):
    responses.get(
        f"{API}/me/tracks",
        json={
            "items": [{"track": sp_track("One")}],
            "next": f"{API}/me/tracks?offset=1&limit=50",
        },
    )
    responses.get(
        f"{API}/me/tracks?offset=1&limit=50",
        json={"items": [{"track": sp_track("Two")}, {"track": None}], "next": None},
    )
    assert [t.title for t in spotify.liked_tracks()] == ["One", "Two"]


@responses.activate
def test_playlists_flag_the_ones_whose_contents_you_cannot_read(spotify):
    def playlist(id, name, owner, total, key="items", **extra):
        return {
            "id": id,
            "name": name,
            "owner": {"id": owner},
            key: {"total": total},
            **extra,
        }

    responses.get(f"{API}/me", json={"id": "me"})
    items = [
        playlist("a", "Mine", "me", 5),
        playlist("b", "Shared", "x", 3, key="tracks", collaborative=True),
        playlist("c", "Followed", "x", 9),
    ]
    responses.get(f"{API}/me/playlists", json={"items": items})
    found = {p.name: (p.track_count, p.readable) for p in spotify.playlists()}
    assert found == {"Mine": (5, True), "Shared": (3, True), "Followed": (9, False)}


@responses.activate
def test_playlist_tracks_read_both_the_new_and_the_old_field_name(spotify):
    items = [{"item": sp_track("New style")}, {"track": sp_track("Old style")}]
    responses.get(f"{API}/playlists/p1/items", json={"items": items})
    assert [t.title for t in spotify.playlist_tracks("p1")] == [
        "New style",
        "Old style",
    ]


@responses.activate
@pytest.mark.parametrize(
    ("message", "explained"),
    [
        ("Forbidden", "own or collaborate"),
        ("Active premium subscription required for the owner of the app", "Premium"),
    ],
)
def test_a_refused_playlist_read_is_explained(spotify, message, explained):
    error = {"error": {"status": 403, "message": message}}
    responses.get(f"{API}/playlists/p1/items", status=403, json=error)
    with pytest.raises(ProviderError, match=explained):
        list(spotify.playlist_tracks("p1"))


@responses.activate
def test_isrc_lookup_is_one_search_per_code_and_leaves_out_what_is_not_found(spotify):
    responses.get(f"{API}/search", json={"tracks": {"items": [sp_track()]}})
    responses.get(f"{API}/search", json={"tracks": {"items": []}})
    found = spotify.lookup_isrcs([ISRC_A, ISRC_B])
    assert list(found) == [ISRC_A]
    assert [sent_query(call)["q"] for call in responses.calls] == [
        [f"isrc:{ISRC_A}"],
        [f"isrc:{ISRC_B}"],
    ]


@responses.activate
def test_find_prefers_the_isrc_and_falls_back_to_a_text_search(spotify):
    responses.get(
        f"{API}/search",
        json={"tracks": {"items": [sp_track("Other Song", "spotify:track:9")]}},
    )
    by_isrc = spotify.find(Track("Song", ["Artist"], isrc=ISRC_A))
    assert by_isrc is not None
    assert by_isrc.method == "isrc"

    responses.replace(
        responses.GET,
        f"{API}/search",
        json={"tracks": {"items": [sp_track("Song", "spotify:track:5")]}},
    )
    by_text = spotify.find(Track("Song", ["Artist"], duration_ms=200000))
    assert by_text is not None
    assert by_text.method == "search"
    assert by_text.track.ids == {"spotify": "spotify:track:5"}


@responses.activate
def test_create_playlist_is_private(spotify):
    responses.post(f"{API}/me/playlists", json={"id": "new"})
    assert spotify.create_playlist("Mix", "About") == "new"
    assert sent_json(responses.calls[0]) == {
        "name": "Mix",
        "description": "About",
        "public": False,
    }


@responses.activate
def test_add_to_playlist_sends_the_uris_and_skips_tracks_without_an_id(spotify):
    responses.post(f"{API}/playlists/p1/items", json={"snapshot_id": "s"})
    spotify.add_to_playlist("p1", [spotify_id(0), spotify_id(1), Track("No id")])
    assert sent_json(responses.calls[0]) == {
        "uris": ["spotify:track:0", "spotify:track:1"]
    }


@responses.activate
def test_add_favorite_tracks_saves_the_uris(spotify):
    responses.put(f"{API}/me/library")
    spotify.add_favorite_tracks([spotify_id(1), spotify_id(2), Track("No id")])
    assert sent_query(responses.calls[0])["uris"] == ["spotify:track:1,spotify:track:2"]


@responses.activate
def test_a_login_without_the_permission_to_change_liked_songs_is_told_to_log_in_again(
    spotify,
):
    refusal = {"error": {"status": 403, "message": "Insufficient client scope"}}
    responses.put(f"{API}/me/library", status=403, json=refusal)
    with pytest.raises(ProviderError, match="Log in again"):
        spotify.add_favorite_tracks([spotify_id(1)])


@responses.activate
def test_albums_are_searched_and_saved_like_tracks(spotify):
    album = {
        "type": "album",
        "name": "Discovery",
        "uri": "spotify:album:5",
        "artists": [{"name": "Daft Punk"}],
    }
    responses.get(f"{API}/search", json={"albums": {"items": [album]}})
    (found,) = spotify.search_albums("Discovery Daft Punk")
    assert (found.title, found.artists, found.ids) == (
        "Discovery",
        ["Daft Punk"],
        {"spotify": "spotify:album:5"},
    )

    responses.put(f"{API}/me/library")
    spotify.add_favorite_albums([found])
    assert sent_query(responses.calls[1])["uris"] == ["spotify:album:5"]
