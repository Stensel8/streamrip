"""Metadata from each source, search results, covers, and the tags put in files."""

import io
import json
import shutil
import threading
import unicodedata
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TSSE
from mutagen.mp4 import MP4
from PIL import Image

from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    ArtistMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
    tag_file,
    tagger,
)
from streamrip.metadata.search_results import SearchResults
from streamrip.metadata.util import qobuz_artists
from streamrip.rip import cover_preview, search_menu

TESTS = Path(__file__).parent
TEST_FLAC = TESTS / "silence.flac"
TEST_M4A = TESTS / "silence.m4a"
TEST_COVER = TESTS / "1x1_pixel.jpg"
QOBUZ_ALBUM = json.loads((TESTS / "qobuz_album_resp.json").read_text(encoding="utf-8"))
QOBUZ_TRACK = json.loads((TESTS / "qobuz_track_resp.json").read_text(encoding="utf-8"))
DEEZER_COVERS = {f"cover_{s}": "u" for s in ("xl", "big", "medium", "small")}
FOLDER = "{title} [{container}] [{bit_depth}B-{sampling_rate}kHz]"


def _tidal_album(**extra):
    return {
        "id": 10,
        "title": "Album",
        "allowStreaming": True,
        "audioQuality": "LOSSLESS",
        "artists": [{"name": "A"}, {"name": "B"}],
        "numberOfTracks": 9,
        "numberOfVolumes": 2,
        "releaseDate": "2019-05-01",
        "cover": "ab-cd",
        **extra,
    }


# --- Qobuz --------------------------------------------------------------------


def test_qobuz_album():
    m = AlbumMetadata.from_qobuz(QOBUZ_ALBUM)
    info = m.info
    assert (info.id, info.quality, info.container) == ("19512572", 3, "FLAC")
    assert (info.sampling_rate, info.bit_depth) == (96, 24)
    assert (info.explicit, info.booklets) == (False, None)
    assert info.label == "Rhino - Warner Records"
    # The edition ("version") is kept in the title so editions do not collide.
    assert (m.album, m.version) == ("Rumours (2001 Remaster)", "2001 Remaster")
    assert (m.albumartist, m.albumcomposer) == ("Fleetwood Mac", "Various Composers")
    assert (m.year, m.date, m.description) == ("1977", "1977-02-04", "")
    # In Qobuz's order, the same on every run (a set's order is not).
    assert m.genre == ["Pop", "Rock"]
    assert not m.covers.empty()
    assert m.compilation is None
    assert m.copyright == (
        "© 1977 Warner Records Inc. ℗ 1977 Warner Records Inc. Marketed by Rhino "
        "Entertainment Company, A Warner Music Group Company."
    )
    assert (m.disctotal, m.tracktotal) == (1, 11)
    assert (m.barcode, m.releasetype) == ("0603497941032", "album")


def test_qobuz_track():
    album = AlbumMetadata.from_qobuz(QOBUZ_TRACK["album"])
    t = TrackMetadata.from_qobuz(album, QOBUZ_TRACK)
    assert (t.info.id, t.info.explicit, t.isrc) == ("216020864", False, "USMRG2384109")
    assert (t.title, t.album, t.artist) == ("Water Tower", album, "The Mountain Goats")
    assert (t.tracknumber, t.discnumber, t.composer) == (9, 1, "John Darnielle")
    # Composers and lyricists from its credits, ReplayGain from audio_info.
    assert (t.composers, t.lyricists) == (["John Darnielle"], ["John Darnielle"])
    assert (t.replaygain_gain, t.replaygain_peak) == (-7.08, 0.936676)
    # Qobuz sent its (C) and (P) lines without their symbols: one line twice.
    assert album.get_copyright() == "2023 Merge Records"


def test_a_qobuz_album_without_its_disc_count_or_tracks_has_one_disc():
    # Qobuz no longer inlines the tracks; without media_count there is nothing.
    resp = {**QOBUZ_ALBUM, "tracks": {"items": []}}
    resp.pop("media_count", None)
    assert AlbumMetadata.from_qobuz(resp).disctotal == 1


@pytest.mark.parametrize(
    ("performers", "artists"),
    [
        (
            (
                "X, Producer - Spiritbox, MainArtist - Tyler, The Creator, "
                "FeaturedArtist - Courtney LaPlante, MainArtist, Vocals"
            ),
            ["Spiritbox", "Courtney LaPlante", "Tyler, The Creator"],
        ),
        # A comma and a suffix, as in "Smith, Jr", belong to the name, not a role.
        (
            "Smith, Jr, MainArtist - Jones, III, FeaturedArtist, Vocals",
            ["Smith, Jr", "Jones, III"],
        ),
        # The clean edition of this track lists its feature first and the
        # explicit one last: both tag the main artist first.
        (
            (
                "Spiritbox, Vocals, FeaturedArtist - "
                "Megan Thee Stallion, MainArtist, Vocals - X, Producer"
            ),
            ["Megan Thee Stallion", "Spiritbox"],
        ),
        (None, []),
    ],
    ids=["roles", "name suffix", "main artist first", "no credits"],
)
def test_qobuz_artists_come_from_the_performers_credits(performers, artists):
    resp = {} if performers is None else {"performers": performers}
    assert qobuz_artists(resp) == artists


# --- Tidal --------------------------------------------------------------------


def test_tidal_album_with_null_copyright_and_no_date():
    # The fix for a null copyright (upstream #979) only reached the copy of
    # this parser used for single tracks; albums still crashed on it.
    m = AlbumMetadata.from_tidal(_tidal_album(copyright=None, releaseDate=None))
    assert m.copyright == ""
    # No "Unkn" (the first four letters of "Unknown") in tags or folder names.
    assert (m.year, m.date) == ("Unknown", None)


@pytest.mark.parametrize(
    ("stream", "quality", "folder"),
    [
        (None, 2, "Album [FLAC] [16B-44.1kHz]"),
        # The album itself only says LOSSLESS; the client adds what the stream is.
        ({"bitDepth": 24, "sampleRate": 96000}, 3, "Album [FLAC] [24B-96kHz]"),
        ({"bitDepth": 24, "sampleRate": 88200}, 3, "Album [FLAC] [24B-88.2kHz]"),
    ],
    ids=["lossless", "hi-res", "fractional kHz"],
)
def test_a_tidal_album_folder_shows_its_stream_format(stream, quality, folder):
    m = AlbumMetadata.from_tidal(_tidal_album(streamQuality=stream))
    assert m.info.quality == quality
    assert m.format_folder_path(FOLDER) == Path(folder)


def test_tidal_album_and_track():
    album = AlbumMetadata.from_tidal(_tidal_album())
    assert (album.albumartist, album.albumartists) == ("A, B", ["A", "B"])
    assert (album.tracktotal, album.disctotal) == (9, 2)
    t = TrackMetadata.from_tidal(
        album,
        {
            "id": 7,
            "title": "Song ",
            "version": "Live",
            "explicit": True,
            "artists": [{"name": "A"}, {"name": "B"}],
            "trackNumber": 3,
            "volumeNumber": 2,
        },
    )
    assert (t.info.id, t.info.explicit, t.title) == ("7", True, "Song (Live)")
    assert (t.artist, t.artists) == ("A, B", ["A", "B"])
    assert (t.tracknumber, t.discnumber, t.lyrics) == (3, 2, "")


@pytest.mark.parametrize(
    ("note", "scale", "key"),
    [
        ("Bb", "MAJOR", "Bb"),
        ("Bb", "MINOR", "Bbm"),
        ("FSharp", "MINOR", "F#m"),
        ("EFlat", "MAJOR", "Eb"),
    ],
)
def test_tidal_bpm_replaygain_key_barcode_and_release_type(note, scale, key):
    album = AlbumMetadata.from_tidal(_tidal_album(upc="00602508053610", type="EP"))
    assert (album.barcode, album.releasetype) == ("00602508053610", "ep")
    t = TrackMetadata.from_tidal(
        album,
        {
            "id": 1,
            "title": "Song",
            "artists": [{"name": "A"}],
            "bpm": 88,
            "replayGain": -10.05,
            "peak": 0.999873,
            "key": note,
            "keyScale": scale,
        },
    )
    assert (t.bpm, t.replaygain_gain, t.replaygain_peak) == (88, -10.05, 0.999873)
    assert t.key == key


@pytest.mark.parametrize(
    ("source", "track", "album"),
    [
        (
            "tidal",
            {
                "id": 99,
                "allowStreaming": True,
                "audioQuality": "HIGH",
                "artists": [{"name": "A"}],
                "streamStartDate": "2020-02-02T00:00:00.000+0000",
                "volumeNumber": 1,
                "copyright": None,
                "album": {"id": 10, "title": "Album", "cover": "ab-cd"},
            },
            ("10", "AAC", False, "A", "2020"),
        ),
        (
            "deezer",  # without the album's track list
            {
                "explicit_lyrics": True,
                "contributors": [
                    {"name": "A", "type": "artist"},
                    {"name": "Producer", "type": "producer"},
                ],
                "album": {"id": 5, "title": "Album", **DEEZER_COVERS},
            },
            ("5", "FLAC", True, "A", "Unknown"),
        ),
    ],
)
def test_a_track_response_gives_its_album(source, track, album):
    m = AlbumMetadata.from_track_resp(track, source)
    info = m.info
    assert m.album == "Album"
    assert (info.id, info.container, info.explicit, m.albumartist, m.year) == album


# --- Deezer and SoundCloud ----------------------------------------------------


def test_deezer_track_metadata():
    resp = {
        "id": 8,
        "title": "Song",
        "track_position": 4,
        "disk_number": 1,
        "contributors": [
            {"name": "A", "type": "artist"},
            {"name": "B", "type": "artist"},
            {"name": "Producer", "type": "producer"},
        ],
        "artist": {"name": "A"},
    }
    album = AlbumMetadata.from_incomplete_deezer_track_resp(
        resp | {"album": {"id": 5, "title": "Album", **DEEZER_COVERS}}
    )
    t = TrackMetadata.from_deezer(album, resp)
    assert (t.info.id, t.info.explicit) == ("8", False)
    assert (t.artist, t.artists, album.albumartist) == ("A, B", ["A", "B"], "A, B")
    assert album.albumartists == ["A", "B"]
    # Without contributors, the main artist.
    del resp["contributors"]
    assert TrackMetadata.from_deezer(album, resp).artists == ["A"]


@pytest.mark.parametrize(
    ("tier", "fmt", "folder"),
    [
        # Without word from the client, Deezer's lossless tier.
        (None, FOLDER, "Album [FLAC] [16B-44.1kHz]"),
        # An MP3 tier has no bit depth: a bracket of only that and the rate goes,
        # instead of saying "[UnknownB-UnknownkHz]".
        (1, FOLDER, "Album [MP3]"),
        (1, "{title} [{bit_depth}bit-{sampling_rate}kHz]", "Album"),
        # One that also holds the container keeps it: it's still the format.
        (
            1,
            "{title} [{container} {bit_depth}B-{sampling_rate}kHz]",
            "Album [MP3 UnknownB-UnknownkHz]",
        ),
        # So does one that says anything else, like an edition.
        (1, "{title} [Deluxe {bit_depth}B]", "Album [Deluxe UnknownB]"),
    ],
)
def test_a_deezer_album_folder_follows_the_stream_quality(tier, fmt, folder):
    resp = {"id": 5, "title": "Album", "artist": {"name": "A"}, "tracks": []}
    resp |= DEEZER_COVERS
    if tier is not None:
        resp["stream_quality"] = tier
    assert AlbumMetadata.from_deezer(resp).format_folder_path(fmt) == Path(folder)


@pytest.mark.parametrize(("bpm", "expected"), [(0, None), (125.6, 126)])
def test_deezer_bpm_barcode_and_release_type(bpm, expected):
    """Deezer's "compile" is a compilation, and a BPM of 0 means unknown."""
    album = AlbumMetadata.from_deezer(
        {
            "id": 5,
            "title": "Album",
            "artist": {"name": "A"},
            "tracks": [],
            "upc": "602445964840",
            "record_type": "compile",
            **DEEZER_COVERS,
        }
    )
    assert (album.barcode, album.releasetype) == ("602445964840", "compilation")
    t = TrackMetadata.from_deezer(
        album,
        {"id": 8, "title": "Song", "track_position": 1, "disk_number": 1, "bpm": bpm},
    )
    assert t.bpm == expected


def test_soundcloud_track_metadata():
    resp = {
        "id": "123|_original_download",
        "title": " Song",
        "user": {"username": "someone", "avatar_url": "https://a/large.jpg"},
        "artwork_url": None,
        "publisher_metadata": {"explicit": True, "isrc": "X"},
    }
    t = TrackMetadata.from_soundcloud(AlbumMetadata.from_soundcloud(resp), resp)
    assert (t.info.id, t.info.explicit) == ("123|_original_download", True)
    assert (t.title, t.artist, t.isrc) == ("Song", "someone", "X")
    assert (t.tracknumber, t.discnumber) == (1, 1)


# --- an artist's discography: the artist's own releases, each listed once -----

ME = 43470


def _album(id, title, artists=(ME,), n=12, version=None, explicit=False, bits=16):
    """A Qobuz album; no `artists` is one without artist credits."""
    album = {
        "id": id,
        "title": title,
        "version": version,
        "tracks_count": n,
        "parental_warning": explicit,
        "maximum_bit_depth": bits,
        "maximum_sampling_rate": 44.1,
    }
    if artists:
        album["artist"] = {"id": artists[0]}
        album["artists"] = [{"id": a} for a in artists]
    return album


def _ids(resp, source="qobuz", prefer_explicit=True):
    return ArtistMetadata.from_resp(resp, source, prefer_explicit).ids


@pytest.mark.parametrize(
    ("albums", "prefer_explicit", "ids"),
    [
        (
            [
                _album("own", "The Kill", n=2),
                _album("cover1", "The Kill", artists=(111,), n=1),
                _album("cover2", "The Kill", artists=(222,), n=1),
                _album("collab", "Wouldn't Change A Thing", artists=(333, ME), n=1),
            ],
            True,
            ["own", "collab"],
        ),
        (
            [
                _album("clean", "LOVE LUST FAITH + DREAMS", explicit=False, bits=24),
                _album("explicit", "LOVE LUST FAITH + DREAMS", explicit=True, bits=24),
            ],
            True,
            ["explicit"],
        ),
        (
            [
                _album("cd", "The Kill (Bury Me)", n=1, bits=16),
                _album("hires", "The Kill (Bury Me)", n=1, bits=24),
            ],
            True,
            ["hires"],
        ),
        (
            [
                _album("std", "This Is War", n=13),
                _album("deluxe", "This Is War", n=15, version="Deluxe"),
                _album("single", "This Is War", n=2),
                _album("remix", "Seasons", n=2, version="JAYEM Remix"),
                _album("acoustic", "Seasons", n=2, version="Acoustic"),
            ],
            True,
            ["std", "deluxe", "single", "remix", "acoustic"],
        ),
        (
            [
                _album("clean", "LOVE LUST FAITH + DREAMS", explicit=False),
                _album("explicit", "LOVE LUST FAITH + DREAMS", explicit=True),
                _album("cover", "The Kill", artists=(111,), n=1),
            ],
            False,
            ["clean", "explicit"],
        ),
        ([_album("x", "Unknown Credits", artists=())], True, ["x"]),
    ],
    ids=[
        "others' covers out, collaborations in",
        "the explicit copy",
        "the higher quality copy",
        "editions and track counts apart",
        "without prefer_explicit, duplicates but not covers",
        "without artist credits",
    ],
)
def test_a_qobuz_discography(albums, prefer_explicit, ids):
    resp = {"id": ME, "name": "30 Seconds To Mars", "albums": {"items": albums}}
    assert _ids(resp, prefer_explicit=prefer_explicit) == ids


def test_deezer_albums_are_left_as_listed():
    resp = {"name": "A", "albums": [{"id": 1, "title": "X"}, {"id": 2, "title": "X"}]}
    assert _ids(resp, source="deezer") == [1, 2]


def test_tidal_keeps_different_versions_but_deduplicates_normalized_versions():
    albums = [
        {
            "id": item_id,
            "title": "Seasons",
            "artist": {"id": ME},
            "numberOfTracks": 2,
            "version": version,
            "explicit": explicit,
        }
        for item_id, version, explicit in [
            ("original", None, False),
            ("remix", "Remix", False),
            ("acoustic-clean", " [Acoustic] ", False),
            ("acoustic-explicit", "(acoustic)", True),
        ]
    ]
    resp = {"name": "A", "albums": albums}
    assert _ids(resp, source="tidal") == ["original", "remix", "acoustic-explicit"]


# --- covers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sizes", "picks"),
    [
        (Covers.SIZES, {"original": "original", "thumbnail": "thumbnail"}),
        (
            ("large", "small"),
            {"original": "large", "small": "small", "thumbnail": "small"},
        ),
        # Only a larger one: that rather than none (SoundCloud has one size).
        (("small",), {"original": "small", "thumbnail": "small"}),
    ],
    ids=["all", "some", "one"],
)
def test_a_cover_size_is_the_nearest_there_is(sizes, picks):
    covers = Covers()
    for size in sizes:
        covers.set_cover(size, f"{size}.jpg", None)
    assert not covers.empty()
    assert covers.largest() == (sizes[0], f"{sizes[0]}.jpg", None)
    assert {size: covers.get_size(size) for size in picks} == {
        size: (pick, f"{pick}.jpg", None) for size, pick in picks.items()
    }


def test_no_covers():
    covers = Covers()
    assert covers.empty()
    with pytest.raises(LookupError):
        covers.largest()
    with pytest.raises(LookupError):
        covers.get_size("original")


@pytest.mark.parametrize("size", Covers.SIZES)
def test_a_soundcloud_cover_is_there_for_every_embed_size(size):
    covers = Covers.from_soundcloud({"artwork_url": "https://i/a-large.jpg"})
    assert covers.get_size(size)[1] == "https://i/a-t500x500.jpg"


# --- search results: they look the same in the menu whatever the source -------

# Trimmed from real Qobuz and Tidal responses, as are the items below.
QOBUZ_HIT = {
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
TIDAL_HIT = {
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


def _one(source, media_type, item):
    """The summary of `item` as one source's search finds it."""
    page = {
        "qobuz": {f"{media_type}s": {"items": [item]}},
        "tidal": {"items": [item]},
        "deezer": {"data": [item]},
    }[source]
    return SearchResults.from_pages(source, media_type, [page]).results[0]


def test_the_same_track_reads_the_same_on_qobuz_and_tidal():
    qobuz = _one("qobuz", "track", QOBUZ_HIT)
    tidal = _one("tidal", "track", TIDAL_HIT)

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


def test_tidal_album_shows_its_type_and_hires_format():
    album = {
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
    album = _one("tidal", "album", album)
    assert album.summarize() == "Mourning by Spiritbox (2026, explicit)"
    assert album.details == [
        ("Released", "2026-09-08"),
        ("Type", "Single"),
        ("Tracks", "1"),
        ("Length", "4:16"),
        # Tidal names no label, but its copyright line does.
        ("Copyright", "© 2026 Pale Chord Music, LLC"),
        # Tidal's sample rate is only in each track's stream info, a request
        # of its own; its hi-res tops out at 192 kHz.
        ("Quality", "FLAC 24-bit, up to 192 kHz"),
    ]


@pytest.mark.parametrize(
    ("source", "artist", "details"),
    [
        # Tidal's artist search has no album count; it used to preview as an id.
        (
            "tidal",
            {"artistRoles": [{"category": "Artist"}, {"category": "Songwriter"}]},
            [("Roles", "Artist, Songwriter")],
        ),
        ("tidal", {"popularity": 74}, [("Popularity", "74/100")]),
        ("qobuz", {"albums_count": 106}, [("Albums", "106")]),
        (
            "deezer",
            {"nb_album": 24, "nb_fan": 76436},
            [("Albums", "24"), ("Fans", "76436")],
        ),
    ],
    ids=["tidal roles", "tidal popularity", "qobuz", "deezer"],
)
def test_artist_previews_are_never_empty(source, artist, details):
    artist = {"id": 1, "name": "Spiritbox", **artist}
    assert _one(source, "artist", artist).details == details


def test_playlists_name_who_made_them():
    playlist = {
        "uuid": "9930c52a",
        "title": "Metal: Best of 2024",
        "numberOfTracks": 50,
        "duration": 12993,
        "creator": {"id": 0},
        "type": "EDITORIAL",
        "description": "The best 2024 releases in metal, including "
        '[wimpLink artistId="14123"]Linkin Park[/wimpLink].',
    }
    tidal = _one("tidal", "playlist", playlist)
    assert tidal.summarize() == "Metal: Best of 2024 by TIDAL"
    assert dict(tidal.details) == {"Tracks": "50", "Length": "3:36:33"}
    assert tidal.description == (
        "The best 2024 releases in metal, including Linkin Park."
    )


def test_cover_fills_the_preview_height_of_a_big_terminal():
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


def test_every_sextant_pattern_gets_the_character_that_fills_it():
    """The character for a pattern of sub-pixels is the one Unicode names after them.

    Cells are numbered 1 to 6 from the top left, row by row; "BLOCK SEXTANT-135" fills
    the first, third and fifth.
    """
    for mask in range(1, 63):
        cells = "".join(str(i + 1) for i in range(6) if mask >> i & 1)
        char = cover_preview._sextant(mask)
        if mask in {21, 42}:  # the halves are older characters
            assert unicodedata.name(char) in {"LEFT HALF BLOCK", "RIGHT HALF BLOCK"}
        else:
            assert unicodedata.name(char) == f"BLOCK SEXTANT-{cells}"


# --- tagging files ------------------------------------------------------------


def _audio(tmp_path, ext) -> Path:
    """A silent file to tag; an MP3's ID3 tag needs no audio stream."""
    path = tmp_path / f"track.{ext}"
    if ext == "mp3":
        path.touch()
    else:
        shutil.copy({"flac": TEST_FLAC, "m4a": TEST_M4A}[ext], path)
    return path


def _tags(path) -> dict:
    """A file's tags as {name: [values]}, in its format's own names."""
    if path.suffix == ".mp3":
        frames = ID3(path, translate=False).items()  # as written, not upgraded
        return {k: f.text for k, f in frames if hasattr(f, "text")}
    if path.suffix == ".m4a":
        return dict(MP4(path).tags)
    return dict(FLAC(path).tags)


@pytest.fixture
def flac_path(tmp_path) -> Path:
    """A copy of the silent FLAC with every tag removed."""
    path = _audio(tmp_path, "flac")
    audio = FLAC(path)
    audio.delete()
    audio.save()
    return path


@pytest.fixture
def sample_metadata() -> TrackMetadata:
    return TrackMetadata(
        TrackInfo(id="12345", explicit=True),
        "testtitle",
        AlbumMetadata(
            AlbumInfo("5678", 4, "flac"),
            "testalbum",
            "testalbumartist",
            "1999",
            ["rock", "pop"],
            Covers(),
            tracktotal=14,
            disctotal=3,
            albumcomposer="testalbumcomposer",
            copyright="(c) stuff (p) other stuff",
            date="1998-02-13",
            description="testdesc",
        ),
        "testartist",
        3,
        1,
        "testcomposer",
        isrc="USABC1234567",
    )


async def test_tag_flac_no_cover(sample_metadata, flac_path):
    await tag_file(flac_path, sample_metadata, None)
    file = FLAC(flac_path)
    assert file["title"][0] == "testtitle"
    assert file["album"][0] == "testalbum"
    assert file["composer"][0] == "testcomposer"
    assert file["artist"][0] == "testartist"
    assert file["albumartist"][0] == "testalbumartist"
    assert file["year"][0] == "1999"
    assert file["genre"][0] == "rock, pop"
    assert file["tracknumber"][0] == "03"
    assert file["discnumber"][0] == "01"
    assert file["copyright"][0] == "© stuff ℗ other stuff"
    assert file["tracktotal"][0] == "14"
    assert file["date"][0] == "1998-02-13"
    assert file["description"][0] == "testdesc"
    assert file["isrc"][0] == "USABC1234567"
    # Empty lyrics (the default) are left out, not written as "".
    assert "lyrics" not in file


async def test_tag_flac_cover(sample_metadata, flac_path):
    await tag_file(flac_path, sample_metadata, TEST_COVER)
    file = FLAC(flac_path)
    assert file["title"][0] == "testtitle"
    picture = file.pictures[0]
    assert picture.data == TEST_COVER.read_bytes()
    # Left at Mutagen's default of 0, some players take that as "no usable
    # cover" without decoding the JPEG themselves to find out.
    assert (picture.width, picture.height, picture.depth) == (1, 1, 24)


async def test_tag_flac_multiple_artists(sample_metadata, flac_path):
    """Several credited artists land as separate ARTIST and ALBUMARTIST fields.

    One "A, B" string shows up in a library as a single artist of that name.
    """
    sample_metadata.artists = ["The Kid LAROI", "Lil Mosey"]
    sample_metadata.album.albumartists = ["Slander", "Spiritbox", "Vastive"]
    await tag_file(flac_path, sample_metadata, None)
    file = FLAC(flac_path)
    assert list(file["artist"]) == ["The Kid LAROI", "Lil Mosey"]
    assert list(file["albumartist"]) == ["Slander", "Spiritbox", "Vastive"]


async def test_tag_m4a(sample_metadata, tmp_path):
    path = _audio(tmp_path, "m4a")
    sample_metadata.artists = ["A", "B"]
    await tag_file(path, sample_metadata, TEST_COVER)
    tags = MP4(path).tags
    assert tags["\xa9nam"] == ["testtitle"]
    assert tags["\xa9ART"] == ["A", "B"]
    assert tags["aART"] == ["testalbumartist"]
    # The composer used to be mapped onto the year's atom and lost.
    assert tags["\xa9wrt"] == ["testcomposer"]
    assert tags["\xa9day"] == ["1999"]
    assert tags["trkn"] == [(3, 14)]
    assert tags["disk"] == [(1, 3)]
    assert tags["cprt"] == ["© stuff ℗ other stuff"]
    assert tags["desc"] == ["testdesc"]
    assert bytes(tags["----:com.apple.iTunes:ISRC"][0]) == b"USABC1234567"
    assert "cpil" not in tags
    assert "\xa9lyr" not in tags
    assert len(tags["covr"]) == 1


async def test_tag_mp3(sample_metadata, tmp_path):
    path = _audio(tmp_path, "mp3")
    sample_metadata.artists = ["A", "B"]
    sample_metadata.album.compilation = "1"
    await tag_file(path, sample_metadata, None)
    tags = ID3(path, translate=False)  # as written, not upgraded to v2.4
    assert tags.version[:2] == (2, 3)
    assert tags["TIT2"].text == ["testtitle"]
    # ID3v2.3 has no multi-value text frames; "/" is its separator.
    assert tags["TPE1"].text == ["A/B"]
    assert tags["TCOM"].text == ["testcomposer"]
    assert tags["TYER"].text == ["1999"]
    assert tags["TRCK"].text == ["3/14"]
    assert tags["TPOS"].text == ["1/3"]
    assert tags["TSRC"].text == ["USABC1234567"]
    assert tags["TCMP"].text == ["1"]
    # The album description used to land in TIT1 (grouping), and empty
    # lyrics in an empty USLT frame.
    assert "TIT1" not in tags
    assert not tags.getall("USLT")


async def test_exclude_only_drops_the_named_tag(sample_metadata, tmp_path):
    # Excluding the composer used to drop the year from M4A files too, as
    # both were mapped onto the same atom.
    path = _audio(tmp_path, "m4a")
    await tag_file(path, sample_metadata, TEST_COVER, exclude=["composer", "cover"])
    tags = MP4(path).tags
    assert "\xa9wrt" not in tags
    assert "covr" not in tags
    assert tags["\xa9day"] == ["1999"]


@pytest.mark.parametrize(
    ("ext", "encoder"), [("flac", "encoder"), ("m4a", "\xa9too"), ("mp3", "TSSE")]
)
async def test_retagging_leaves_only_the_new_tags(
    sample_metadata, tmp_path, ext, encoder
):
    # tag_file's tags are meant to be the whole story. A remux (Deezer's
    # download goes through ffmpeg) leaves its own encoder tag on the file,
    # and may copy the cover in before a converted file is tagged again:
    # FLAC's add_picture appends where ID3 and MP4 replace, so a re-tagged
    # FLAC used to carry the cover twice.
    path = _audio(tmp_path, ext)
    if ext == "mp3":
        id3 = ID3()
        id3.add(TSSE(encoding=3, text="Lavf60.3.100"))
        id3.save(path)
    else:
        audio = (FLAC if ext == "flac" else MP4)(path)
        audio[encoder] = ["Lavf60.3.100"]
        audio.save()

    await tag_file(path, sample_metadata, TEST_COVER)
    await tag_file(path, sample_metadata, TEST_COVER)

    assert encoder not in _tags(path)
    if ext == "flac":
        covers = FLAC(path).pictures
    elif ext == "m4a":
        covers = MP4(path).tags["covr"]
    else:
        covers = ID3(path).getall("APIC")
    assert len(covers) == 1


async def test_tag_flac_without_a_tag_block(sample_metadata, tmp_path):
    # A FLAC with no Vorbis comment block at all has mutagen's tags at None:
    # clearing them raised AttributeError, failing every Tidal and Qobuz
    # track in 2.6.7 and 2.6.8 (issue #91).
    path = _audio(tmp_path, "flac")
    audio = FLAC(path)
    audio.metadata_blocks = [b for b in audio.metadata_blocks if b.code != 4]
    audio.tags = None
    audio.save()
    assert FLAC(path).tags is None

    await tag_file(path, sample_metadata, TEST_COVER)

    file = FLAC(path)
    assert file["title"] == ["testtitle"]
    assert len(file.pictures) == 1


FREE = "----:com.apple.iTunes:"


@pytest.mark.parametrize(
    ("ext", "expected"),
    [
        (
            "flac",
            {
                "label": ["Label"],
                "barcode": ["0123"],
                "releasetype": ["ep"],
                "bpm": ["120"],
                "initialkey": ["F#m"],
                "composer": ["C1", "C2"],
                "lyricist": ["L1", "L2"],
                "itunesadvisory": ["1"],
                "replaygain_track_gain": ["-7.08 dB"],
                "replaygain_track_peak": ["0.936676"],
            },
        ),
        (
            "mp3",
            {
                "TPUB": ["Label"],
                "TXXX:BARCODE": ["0123"],
                "TXXX:MusicBrainz Album Type": ["ep"],
                "TBPM": ["120"],
                "TKEY": ["F#m"],
                "TCOM": ["C1/C2"],
                "TEXT": ["L1/L2"],
                "TXXX:ITUNESADVISORY": ["1"],
                "TXXX:REPLAYGAIN_TRACK_GAIN": ["-7.08 dB"],
                "TXXX:REPLAYGAIN_TRACK_PEAK": ["0.936676"],
            },
        ),
        (
            "m4a",
            {
                FREE + "LABEL": [b"Label"],
                FREE + "BARCODE": [b"0123"],
                FREE + "MusicBrainz Album Type": [b"ep"],
                "tmpo": [120],
                FREE + "initialkey": [b"F#m"],
                "\xa9wrt": ["C1", "C2"],
                FREE + "LYRICIST": [b"L1", b"L2"],
                "rtng": [1],
                FREE + "replaygain_track_gain": [b"-7.08 dB"],
                FREE + "replaygain_track_peak": [b"0.936676"],
            },
        ),
    ],
    ids=["flac", "mp3", "m4a"],
)
async def test_label_barcode_release_type_bpm_key_credits_and_replaygain(
    sample_metadata, tmp_path, ext, expected
):
    # Where MusicBrainz Picard puts them, so players and Navidrome read them.
    path = _audio(tmp_path, ext)
    m = sample_metadata
    m.album.info.label, m.album.barcode, m.album.releasetype = "Label", "0123", "ep"
    m.composers, m.composer = ["C1", "C2"], "C1, C2"
    m.lyricists, m.bpm, m.key = ["L1", "L2"], 120, "F#m"
    m.replaygain_gain, m.replaygain_peak = -7.08, 0.936676
    await tag_file(path, m, None)

    tags = _tags(path)
    assert {name: tags.get(name) for name in expected} == expected


async def test_unknown_or_meaningless_values_are_not_tagged(sample_metadata, flac_path):
    sample_metadata.info.explicit = False  # a clean track gets no advisory
    album = sample_metadata.album
    album.year, album.date = "Unknown", None
    album.tracktotal, album.disctotal = 1, 0  # fallbacks for track 3 of disc 1
    await tag_file(flac_path, sample_metadata, None)
    t = FLAC(flac_path)
    for name in (
        "itunesadvisory",
        "bpm",
        "label",
        "replaygain_track_gain",
        "date",
        "year",
        "tracktotal",
        "totaltracks",
        "disctotal",
    ):
        assert name not in t, name
    assert t["tracknumber"] == ["03"]


async def test_tagging_runs_off_the_event_loop(monkeypatch, sample_metadata, flac_path):
    """A FLAC whose tags outgrow their room is rewritten whole: not on the loop."""
    threads = []
    monkeypatch.setattr(
        tagger, "_tag_file", lambda *_: threads.append(threading.current_thread())
    )
    await tag_file(flac_path, sample_metadata, None)
    assert threads
    assert threads[0] is not threading.main_thread()
