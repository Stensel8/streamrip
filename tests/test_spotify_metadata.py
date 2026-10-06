"""Spotify's responses as streamrip's metadata, and Spotify URLs as pending items."""

from types import SimpleNamespace

import pytest

from streamrip.config import Config
from streamrip.db import download_key, split_download_key
from streamrip.media import PendingAlbum, PendingArtist, PendingPlaylist, PendingSingle
from streamrip.metadata import (
    AlbumMetadata,
    ArtistMetadata,
    Covers,
    PlaylistMetadata,
    TrackMetadata,
)
from streamrip.rip.parse_url import parse_url

ID = "4uLU6hMCjMI75M1A2tKUQC"

IMAGES = [
    {"url": "https://i.scdn.co/image/64", "width": 64},
    {"url": "https://i.scdn.co/image/640", "width": 640},
    {"url": "https://i.scdn.co/image/300", "width": 300},
]


def album_resp(**extra):
    return {
        "id": "album1",
        "name": "Random Access Memories",
        "artists": [{"name": "Daft Punk"}],
        "release_date": "2013-05-17",
        "images": IMAGES,
        "total_tracks": 13,
        "genres": ["electronic"],
        "copyrights": [
            {"text": "2013 Daft Life Limited", "type": "C"},
            {"text": "\u2117 2013 Daft Life Limited", "type": "P"},
            {"text": "  ", "type": "C"},
        ],
        "container": "AAC",
    } | extra


def track_resp(**extra):
    return {
        "id": ID,
        "name": "Get Lucky ",
        "artists": [{"name": "Daft Punk"}, {"name": "Pharrell Williams"}],
        "album": album_resp(),
        "track_number": 8,
        "disc_number": 1,
        "explicit": True,
        "external_ids": {"isrc": "gbdum1300019"},
    } | extra


# --- albums ------------------------------------------------------------------


def test_an_album_is_lossy_and_says_which_container():
    meta = AlbumMetadata.from_album_resp(album_resp(), "spotify")

    assert meta is not None
    assert meta.album == "Random Access Memories"
    assert meta.albumartist == "Daft Punk"
    assert meta.albumartists == ["Daft Punk"]
    assert meta.year == "2013"
    assert meta.date == "2013-05-17"
    assert meta.genre == ["electronic"]
    assert meta.tracktotal == 13
    assert meta.info.id == "album1"
    assert meta.info.container == "AAC"
    assert meta.info.bit_depth is None
    assert meta.info.sampling_rate is None


def test_an_album_folder_has_no_empty_quality_bracket():
    meta = AlbumMetadata.from_album_resp(album_resp(), "spotify")

    folder = meta.format_folder_path(
        "{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]"
    )

    assert folder == "Daft Punk - Random Access Memories (2013) [AAC]"


def test_the_copyright_lines_are_joined_and_marked():
    meta = AlbumMetadata.from_album_resp(album_resp(), "spotify")

    assert meta.copyright == "(C) 2013 Daft Life Limited; \u2117 2013 Daft Life Limited"
    assert (
        meta.get_copyright()
        == "\u00a9 2013 Daft Life Limited; \u2117 2013 Daft Life Limited"
    )


@pytest.mark.parametrize(
    ("date", "year"), [("2013", "2013"), ("2013-05", "2013"), (None, "Unknown")]
)
def test_a_release_date_can_be_a_year_only(date, year):
    meta = AlbumMetadata.from_album_resp(album_resp(release_date=date), "spotify")

    assert meta.year == year


def test_the_discs_of_an_album_come_from_its_tracks():
    tracks = [{"id": "a", "disc_number": 1}, {"id": "b", "disc_number": 2}]

    meta = AlbumMetadata.from_album_resp(album_resp(tracks=tracks), "spotify")

    assert meta.disctotal == 2
    assert AlbumMetadata.from_album_resp(album_resp(), "spotify").disctotal == 1


def test_the_album_of_a_track_knows_at_least_the_disc_the_track_is_on():
    meta = AlbumMetadata.from_track_resp(track_resp(disc_number=2), "spotify")

    assert meta is not None
    assert meta.disctotal == 2
    assert meta.album == "Random Access Memories"


def test_an_album_without_a_container_is_aac():
    resp = album_resp()
    del resp["container"]

    assert AlbumMetadata.from_album_resp(resp, "spotify").info.container == "AAC"


# --- covers ------------------------------------------------------------------


def test_the_covers_take_spotifys_three_sizes():
    covers = Covers.from_spotify({"images": IMAGES})

    assert covers.largest()[1] == "https://i.scdn.co/image/640"
    assert covers.get_size("original")[1] == "https://i.scdn.co/image/640"
    assert covers.get_size("large")[1] == "https://i.scdn.co/image/640"
    assert covers.get_size("small")[1] == "https://i.scdn.co/image/300"
    assert covers.get_size("thumbnail")[1] == "https://i.scdn.co/image/64"


def test_one_image_serves_every_size():
    covers = Covers.from_spotify({"images": IMAGES[:1]})

    assert {covers.get_size(s)[1] for s in Covers.SIZES} == {
        "https://i.scdn.co/image/64"
    }


def test_no_images_means_no_cover():
    assert Covers.from_spotify({"images": []}).empty()
    assert Covers.from_spotify({}).empty()


# --- tracks ------------------------------------------------------------------


def test_a_track_is_tagged_from_spotifys_response():
    album = AlbumMetadata.from_track_resp(track_resp(), "spotify")

    meta = TrackMetadata.from_resp(album, "spotify", track_resp())

    assert meta is not None
    assert meta.title == "Get Lucky"
    assert meta.artist == "Daft Punk, Pharrell Williams"
    assert meta.artists == ["Daft Punk", "Pharrell Williams"]
    assert meta.tracknumber == 8
    assert meta.discnumber == 1
    assert meta.isrc == "GBDUM1300019"
    assert meta.info.id == ID
    assert meta.info.explicit is True


def test_a_track_without_an_isrc_or_artists_still_has_tags():
    resp = track_resp(artists=[], external_ids={})
    album = AlbumMetadata.from_track_resp(resp, "spotify")

    meta = TrackMetadata.from_resp(album, "spotify", resp)

    assert meta.isrc is None
    assert meta.artists is None
    assert meta.artist == "Daft Punk"  # the album's


# --- playlists and artists ---------------------------------------------------


def test_a_playlist_is_its_name_and_the_ids_of_its_tracks():
    resp = {"name": "CC Mix", "tracks": [{"id": "a"}, {"id": "b"}]}

    meta = PlaylistMetadata.from_resp(resp, "spotify")

    assert meta.name == "CC Mix"
    assert meta.ids == ["a", "b"]


def test_an_artist_is_its_name_and_the_ids_of_its_albums():
    resp = {"name": "Kevin MacLeod", "albums": [{"id": "x"}, {"id": "y"}]}

    meta = ArtistMetadata.from_resp(resp, "spotify")

    assert meta.name == "Kevin MacLeod"
    assert meta.ids == ["x", "y"]


# --- the downloads table -----------------------------------------------------


def test_spotify_tracks_are_recorded_under_their_source():
    assert download_key("spotify", ID) == f"spotify_{ID}"
    assert split_download_key(f"spotify_{ID}") == ("spotify", ID)


# --- urls --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "media_type"),
    [
        (f"https://open.spotify.com/track/{ID}", "track"),
        (f"https://open.spotify.com/track/{ID}?si=0123456789abcdef", "track"),
        (f"https://open.spotify.com/intl-nl/album/{ID}", "album"),
        (f"https://open.spotify.com/intl-pt-BR/artist/{ID}?si=x", "artist"),
        (f"https://open.spotify.com/embed/playlist/{ID}", "playlist"),
        (f"https://open.spotify.com/user/someone/playlist/{ID}", "playlist"),
        (f"http://open.spotify.com/album/{ID}", "album"),
        (f"spotify:track:{ID}", "track"),
        (f"spotify:user:someone:playlist:{ID}", "playlist"),
        (f"  https://open.spotify.com/track/{ID}\n", "track"),
    ],
)
def test_spotify_links_and_uris_are_recognised(url, media_type):
    parsed = parse_url(url)

    assert parsed is not None
    assert parsed.source == "spotify"
    assert parsed.match.groups() == (media_type, ID)


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com/track/tooshort",
        f"https://open.spotify.com/track/{ID}x",  # 23 characters
        f"https://open.spotify.com/show/{ID}",  # podcasts have no tracks to match
        f"https://open.spotify.com/episode/{ID}",
        f"https://example.com/track/{ID}",
        f"spotify:show:{ID}",
        "https://open.spotify.com/",
    ],
)
def test_other_links_are_not_spotify_items(url):
    assert parse_url(url) is None


@pytest.mark.parametrize(
    ("url", "pending_type"),
    [
        (f"https://open.spotify.com/track/{ID}", PendingSingle),
        (f"https://open.spotify.com/album/{ID}", PendingAlbum),
        (f"https://open.spotify.com/artist/{ID}", PendingArtist),
        (f"https://open.spotify.com/playlist/{ID}", PendingPlaylist),
    ],
)
async def test_a_spotify_url_becomes_the_pending_item_of_its_kind(url, pending_type):
    client = SimpleNamespace(source="spotify")
    config = Config.defaults()

    pending = await parse_url(url).into_pending(client, config, None)

    assert isinstance(pending, pending_type)
    assert pending.id == ID
    assert pending.client is client
