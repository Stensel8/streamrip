"""Search results look the same in the menu whatever the source.

The items below are trimmed from real Qobuz, Tidal and Deezer responses.
"""

import io

from PIL import Image

from streamrip.metadata.search_results import SearchResults
from streamrip.rip import cover_preview, search_menu

QOBUZ_TRACK = {
    "id": 380483157,
    "title": "Circle With Me",
    "version": None,
    "duration": 233,
    "track_number": 11,
    "parental_warning": False,
    "maximum_bit_depth": 16,
    "maximum_sampling_rate": 44.1,
    "release_date_original": "2021-09-17",
    "performer": {"name": "Spiritbox", "id": 6006722},
    "performers": "Spiritbox, MainArtist - Courtney LaPlante, ComposerLyricist",
    "album": {
        "title": "Eternal Blue",
        "image": {"thumbnail": "https://static.qobuz.com/t9cn3qpdmzgy9_50.jpg"},
        "genre": {"name": "Metal"},
        "label": {"name": "Rise Records"},
    },
}
TIDAL_TRACK = {
    "id": 486406478,
    "title": "Circle With Me",
    "version": None,
    "duration": 234,
    "trackNumber": 11,
    "explicit": False,
    "streamStartDate": "2021-09-17T00:00:00.000+0000",
    "mediaMetadata": {"tags": ["LOSSLESS"]},
    "artist": {"name": "Spiritbox"},
    "artists": [{"name": "Spiritbox", "type": "MAIN"}],
    "album": {"title": "Eternal Blue", "cover": "cabed6a2-cd16-4de7-b8b5-2e498a00d35e"},
}


def _one(source, media_type, page):
    """The first result of one page of search results."""
    return SearchResults.from_pages(source, media_type, [page]).results[0]


def test_the_same_track_reads_the_same_on_qobuz_and_tidal():
    """The same track reads alike whichever source it comes from."""
    qobuz = _one("qobuz", "track", {"tracks": {"items": [QOBUZ_TRACK]}})
    tidal = _one("tidal", "track", {"items": [TIDAL_TRACK]})

    line = "Circle With Me by Spiritbox (2021)"
    assert qobuz.summarize() == tidal.summarize() == line
    for summary in (qobuz, tidal):
        details = dict(summary.details)
        assert details["Album"] == "Eternal Blue"
        assert details["Track"] == "11"
        assert details["Released"] == "2021-09-17"
        assert details["Quality"] == "FLAC 16-bit / 44.1 kHz"
    # What only Qobuz knows is shown too.
    assert (dict(qobuz.details)["Genre"], dict(qobuz.details)["Label"]) == (
        "Metal",
        "Rise Records",
    )
    assert qobuz.image_url == "https://static.qobuz.com/t9cn3qpdmzgy9_50.jpg"
    assert tidal.image_url == (
        "https://resources.tidal.com/images/cabed6a2/cd16/4de7/b8b5/2e498a00d35e/"
        "640x640.jpg"
    )


def test_tidal_says_hires_without_a_sample_rate():
    """Tidal's hi-res quality is shown without a sample rate.

    The rate is only in a track's stream info. Tidal's hi-res tops out at 24-bit / 192
    kHz, and that much is shown without a request.
    """
    tidal = _one("tidal", "track", {"items": [TIDAL_TRACK]})
    hires = {**TIDAL_TRACK, "mediaMetadata": {"tags": ["LOSSLESS", "HIRES_LOSSLESS"]}}
    hires = _one("tidal", "track", {"items": [hires]})
    assert dict(tidal.details)["Quality"] == "FLAC 16-bit / 44.1 kHz"
    assert dict(hires.details)["Quality"] == "FLAC 24-bit, up to 192 kHz"


def test_tidal_album_shows_its_type_and_hires_format():
    """A Tidal album's preview shows its type and its hi-res format."""
    album = _one(
        "tidal",
        "album",
        {
            "items": [
                {
                    "id": 558087529,
                    "title": "Mourning",
                    "duration": 256,
                    "numberOfTracks": 1,
                    "numberOfVolumes": 1,
                    "releaseDate": "2026-09-08",
                    "type": "SINGLE",
                    "explicit": True,
                    "mediaMetadata": {"tags": ["LOSSLESS", "HIRES_LOSSLESS"]},
                    "copyright": "© 2026 Pale Chord Music, LLC",
                    "artist": {"name": "Spiritbox"},
                }
            ]
        },
    )
    assert album.summarize() == "Mourning by Spiritbox (2026, explicit)"
    assert album.details == [
        ("Released", "2026-09-08"),
        ("Type", "Single"),
        ("Tracks", "1"),
        ("Length", "4:16"),
        # Tidal names no label, but its copyright line does.
        ("Copyright", "© 2026 Pale Chord Music, LLC"),
        ("Quality", "FLAC 24-bit, up to 192 kHz"),
    ]


def test_artist_previews_are_never_empty():
    """Artist previews are never empty.

    Tidal's artist search has no album count; it used to preview as an id.
    """
    tidal = _one(
        "tidal",
        "artist",
        {
            "items": [
                {
                    "id": 9163057,
                    "name": "Spiritbox",
                    "artistRoles": [{"category": "Artist"}, {"category": "Songwriter"}],
                }
            ]
        },
    )
    qobuz = _one(
        "qobuz",
        "artist",
        {
            "artists": {
                "items": [{"id": 6006722, "name": "Spiritbox", "albums_count": 106}]
            }
        },
    )
    deezer = _one(
        "deezer",
        "artist",
        {"data": [{"id": 1, "name": "Spiritbox", "nb_album": 24, "nb_fan": 76436}]},
    )
    assert tidal.details == [("Roles", "Artist, Songwriter")]
    popular = {"id": 1, "name": "Spiritbox", "popularity": 74}
    assert _one("tidal", "artist", {"items": [popular]}).details == [
        ("Popularity", "74/100")
    ]
    assert qobuz.details == [("Albums", "106")]
    assert deezer.details == [("Albums", "24"), ("Fans", "76436")]


def test_playlists_name_who_made_them():
    """A playlist's preview names its creator."""
    tidal = _one(
        "tidal",
        "playlist",
        {
            "items": [
                {
                    "uuid": "9930c52a",
                    "title": "Metal: Best of 2024",
                    "numberOfTracks": 50,
                    "duration": 12993,
                    "creator": {"id": 0},
                    "type": "EDITORIAL",
                    "description": "The best 2024 releases in metal, including "
                    '[wimpLink artistId="14123"]Linkin Park[/wimpLink].',
                }
            ]
        },
    )
    assert tidal.summarize() == "Metal: Best of 2024 by TIDAL"
    assert dict(tidal.details) == {"Tracks": "50", "Length": "3:36:33"}
    assert tidal.description == (
        "The best 2024 releases in metal, including Linkin Park."
    )


def test_cover_fills_the_preview_height_of_a_big_terminal():
    """On a big terminal the cover fills the height of the preview."""
    assert search_menu.cover_size(200, 60) == 28  # 60 * 0.5, less the border
    assert search_menu.cover_size(60, 60) == 0  # no room beside the details


def test_cover_draws_an_edge_inside_a_character():
    """A vertical edge through the middle of a character stays an edge.

    Half blocks could only split a character top from bottom, and blurred it into an
    average of both sides.
    """
    image = Image.new("RGB", (4, 4), "white")
    for x in (0, 2):  # each character's left half black
        image.paste((0, 0, 0), (x, 0, x + 1, 4))
    buf = io.BytesIO()
    image.save(buf, "PNG")

    (row,) = cover_preview.cover_rows(buf.getvalue(), 1)

    assert row.plain == "▌▌"
    assert {span.style.color.triplet for span in row.spans} == {(0, 0, 0)}
