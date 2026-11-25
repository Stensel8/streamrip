"""Metadata from each source, search results, covers, and the tags put in files."""

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from PIL import Image

from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    ArtistMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
    tag_file,
)
from streamrip.metadata.search_results import SearchResults
from streamrip.metadata.util import (
    disc_track_totals,
    edition_of,
    gtin13,
    qobuz_artists,
)
from streamrip.rip import cover_preview
from streamrip.utils.ffmpeg_utils import find_ffmpeg

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


def test_qobuz_album():
    m = AlbumMetadata.from_qobuz(QOBUZ_ALBUM)
    info = m.info
    assert (info.id, info.quality, info.container) == ("19512572", 3, "FLAC")
    assert (info.sampling_rate, info.bit_depth) == (96, 24)
    assert info.label == "Rhino - Warner Records"
    # The edition ("version") is kept in the title so editions do not collide.
    assert (m.album, m.version) == ("Rumours (2001 Remaster)", "2001 Remaster")
    assert (m.albumartist, m.albumcomposer) == ("Fleetwood Mac", "Various Composers")
    assert (m.year, m.date) == ("1977", "1977-02-04")
    assert m.genre == ["Pop", "Rock"]  # in Qobuz's order, the same on every run
    assert (m.disctotal, m.tracktotal) == (1, 11)
    assert (m.barcode, m.releasetype) == ("0603497941032", "album")


@pytest.mark.parametrize(
    ("source", "tier", "folder"),
    [
        ("qobuz", 4, "FLAC [24B-192kHz]"),
        ("qobuz", 1, "MP3"),
        ("tidal", 3, "FLAC [24B-192kHz]"),
        ("tidal", 1, "AAC"),
    ],
)
def test_an_album_folder_shows_the_quality_that_is_downloaded(source, tier, folder):
    # Issue #92: it showed the best the service has, whatever was asked for.
    if source == "qobuz":
        m = AlbumMetadata.from_qobuz(QOBUZ_ALBUM | {"maximum_sampling_rate": 192})
    else:
        stream = {"bitDepth": 24, "sampleRate": 192000}
        m = AlbumMetadata.from_tidal(_tidal_album(streamQuality=stream))
    fmt = "{container} [{bit_depth}B-{sampling_rate}kHz]"
    assert m.at_quality(tier, source).format_folder_path(fmt) == Path(folder)


def test_a_qobuz_description_is_tagged_without_its_html():
    html = "<p>Gone are the times <em>Villains</em> &amp; more.</p>"
    album = AlbumMetadata.from_qobuz(QOBUZ_ALBUM | {"description": html})
    assert album.description == "Gone are the times Villains & more."


@pytest.mark.parametrize(
    "code",
    ["0093624834892", "093624834892", "00093624834892", " 0936-2483-4892 "],
    ids=["13 digits", "a UPC", "a GTIN-14", "with spaces and dashes"],
)
def test_one_product_has_one_barcode_whichever_way_a_service_writes_it(code):
    assert gtin13(code) == "0093624834892"


@pytest.mark.parametrize(
    ("title", "edition"),
    [
        ("From Zero (Deluxe Edition)", "Deluxe Edition"),
        ("Hybrid Theory (Bonus Edition)", "Bonus Edition"),
        ("Abbey Road [2019 Remaster]", "2019 Remaster"),
        ("From Zero", None),
        ("Live (At The Edition Hall) EP", None),  # not at the end
        ("Numb (Radio Edit)", None),
        ("Act (Expedition)", None),  # "edition" inside a word
        ("Hits (Remix Versions)", "Remix Versions"),  # more than one
    ],
)
def test_the_edition_is_read_from_the_end_of_a_title(title, edition):
    assert edition_of(title) == edition


def test_a_credit_in_capitals_is_spelt_as_the_services_artist_is():
    # Qobuz credits "LINKIN PARK, MainArtist" for an artist it calls Linkin Park.
    track = QOBUZ_TRACK | {
        "performer": {"name": "Linkin Park"},
        "performers": "LINKIN PARK, MainArtist, Writer - BRAD DELSON, Guitar",
    }
    album = AlbumMetadata.from_qobuz(QOBUZ_TRACK["album"])

    assert TrackMetadata.from_qobuz(album, track).artist == "Linkin Park"


@pytest.mark.parametrize(
    ("title", "version", "expected"),
    [
        ("Demons", "Demo", "Demons (Demo)"),  # not a word of the name
        ("Live Wire", "Live", "Live Wire (Live)"),
        ("Song (Live)", "live", "Song (Live)"),
        ("Song - Live", "Live", "Song - Live"),
    ],
)
def test_a_qobuz_version_is_added_unless_the_title_says_it(title, version, expected):
    album = AlbumMetadata.from_qobuz(QOBUZ_TRACK["album"])
    track = QOBUZ_TRACK | {"title": title, "version": version}
    assert TrackMetadata.from_qobuz(album, track).title == expected


@pytest.mark.parametrize(
    ("resp", "totals"),
    [
        (
            {"tracks": [{"volumeNumber": 1}, {"volumeNumber": 2}, {"volumeNumber": 2}]},
            {1: 1, 2: 2},
        ),
        ({"tracks": {"items": [{"media_number": 1}]}}, {1: 1}),
        ({"track_ids": ["1", "2"]}, {}),  # Qobuz, which no longer lists its tracks
        ({"tracks": [{"id": 1}]}, {}),  # a track without its disc
    ],
)
def test_the_tracks_of_each_disc_are_counted(resp, totals):
    assert disc_track_totals(resp) == totals


def test_qobuz_track():
    album = AlbumMetadata.from_qobuz(QOBUZ_TRACK["album"])
    t = TrackMetadata.from_qobuz(album, QOBUZ_TRACK)
    assert (t.info.id, t.isrc) == ("216020864", "USMRG2384109")
    assert (t.title, t.artist) == ("Water Tower", "The Mountain Goats")
    assert (t.tracknumber, t.discnumber, t.composer) == (9, 1, "John Darnielle")
    assert (t.composers, t.lyricists) == (["John Darnielle"], ["John Darnielle"])
    assert (t.replaygain_gain, t.replaygain_peak) == (-7.08, 0.936676)
    # Qobuz sent its (C) and (P) lines without their symbols: one line twice.
    assert album.get_copyright() == "2023 Merge Records"


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
        # The clean edition lists its feature first and the explicit one last:
        # both tag the main artist first.
        (
            (
                "Spiritbox, Vocals, FeaturedArtist - "
                "Megan Thee Stallion, MainArtist, Vocals - X, Producer"
            ),
            ["Megan Thee Stallion", "Spiritbox"],
        ),
    ],
    ids=["roles", "main artist first"],
)
def test_qobuz_artists_come_from_the_performers_credits(performers, artists):
    assert qobuz_artists({"performers": performers}) == artists


@pytest.mark.parametrize(
    ("title", "version", "album"),
    [
        ("Seasons", "Remix", "Seasons (Remix)"),
        # Tidal puts some editions in the title as well: once is enough.
        ("Seasons (Deluxe Edition)", "Deluxe Edition", "Seasons (Deluxe Edition)"),
    ],
)
def test_a_tidal_edition_is_in_the_title_as_qobuz_has_it(title, version, album):
    m = AlbumMetadata.from_tidal(_tidal_album(title=title, version=version))
    assert (m.album, m.version) == (album, version)


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
    ],
    ids=["lossless", "hi-res"],
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
    assert (t.tracknumber, t.discnumber) == (3, 2)


@pytest.mark.parametrize(
    ("note", "scale", "key"), [("Bb", "MINOR", "Bbm"), ("FSharp", "MINOR", "F#m")]
)
def test_tidal_bpm_replaygain_key_barcode_and_release_type(note, scale, key):
    album = AlbumMetadata.from_tidal(_tidal_album(upc="00602508053610", type="EP"))
    assert (album.barcode, album.releasetype) == ("0602508053610", "ep")
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


def test_a_track_response_gives_its_album():
    track = {
        "id": 99,
        "allowStreaming": True,
        "audioQuality": "HIGH",
        "artists": [{"name": "A"}],
        "streamStartDate": "2020-02-02T00:00:00.000+0000",
        "volumeNumber": 1,
        "copyright": None,
        "album": {"id": 10, "title": "Album", "cover": "ab-cd"},
    }
    m = AlbumMetadata.from_track_resp(track, "tidal")
    assert (m.info.id, m.album, m.albumartist, m.year) == ("10", "Album", "A", "2020")
    assert (m.info.container, m.info.explicit) == ("AAC", False)


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
    # Deezer's "compile" is a compilation, and a BPM of 0 means unknown.
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
    assert (album.barcode, album.releasetype) == ("0602445964840", "compilation")
    resp = {"id": 8, "title": "Song", "track_position": 1, "disk_number": 1}
    assert TrackMetadata.from_deezer(album, resp | {"bpm": bpm}).bpm == expected


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


# An artist's discography: the artist's own releases, each listed once.

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


def _ids(resp, source="qobuz"):
    return ArtistMetadata.from_resp(resp, source, True).ids


@pytest.mark.parametrize(
    ("albums", "ids"),
    [
        (
            [
                _album("own", "The Kill", n=2),
                _album("cover1", "The Kill", artists=(111,), n=1),
                _album("cover2", "The Kill", artists=(222,), n=1),
                _album("collab", "Wouldn't Change A Thing", artists=(333, ME), n=1),
            ],
            ["own", "collab"],
        ),
        (
            [
                _album("clean", "LOVE LUST FAITH + DREAMS", explicit=False),
                _album("explicit", "LOVE LUST FAITH + DREAMS", explicit=True),
            ],
            ["explicit"],
        ),
        (
            [
                _album("cd", "The Kill (Bury Me)", n=1, bits=16),
                _album("hires", "The Kill (Bury Me)", n=1, bits=24),
            ],
            ["hires"],
        ),
        (
            [
                _album("std", "This Is War", n=13),
                _album("deluxe", "This Is War", n=15, version="Deluxe"),
                _album("remix", "Seasons", n=2, version="JAYEM Remix"),
                _album("acoustic", "Seasons", n=2, version="Acoustic"),
            ],
            ["std", "deluxe", "remix", "acoustic"],
        ),
        ([_album("x", "Unknown Credits", artists=())], ["x"]),
    ],
    ids=[
        "others' covers out, collaborations in",
        "the explicit copy",
        "the higher quality copy",
        "editions apart",
        "without artist credits",
    ],
)
def test_a_qobuz_discography(albums, ids):
    resp = {"id": ME, "name": "30 Seconds To Mars", "albums": {"items": albums}}
    assert _ids(resp) == ids


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


@pytest.mark.parametrize(
    ("sizes", "picks"),
    [
        (
            ("large", "small"),
            {"original": "large", "small": "small", "thumbnail": "small"},
        ),
        # Only a larger one: that rather than none (SoundCloud has one size).
        (("small",), {"original": "small", "thumbnail": "small"}),
    ],
    ids=["some", "one"],
)
def test_a_cover_size_is_the_nearest_there_is(sizes, picks):
    covers = Covers()
    for size in sizes:
        covers.set_cover(size, f"{size}.jpg", None)
    assert covers.largest() == (sizes[0], f"{sizes[0]}.jpg", None)
    assert {size: covers.get_size(size) for size in picks} == {
        size: (pick, f"{pick}.jpg", None) for size, pick in picks.items()
    }


def test_a_soundcloud_cover_is_there_for_every_embed_size():
    covers = Covers.from_soundcloud({"artwork_url": "https://i/a-large.jpg"})
    urls = {covers.get_size(size)[1] for size in Covers.SIZES}
    assert urls == {"https://i/a-t500x500.jpg"}


# The same track as the menu of a search shows it, whatever the source (trimmed
# from real Qobuz and Tidal responses).
QOBUZ_HIT = {
    "id": 380483157,
    "title": "Circle With Me",
    "duration": 233,
    "parental_warning": False,
    "maximum_bit_depth": 16,
    "maximum_sampling_rate": 44.1,
    "release_date_original": "2021-09-17",
    "performer": {"name": "Spiritbox", "id": 6006722},
    "album": {
        "title": "Eternal Blue",
        "image": {"thumbnail": "https://static.qobuz.com/t9cn3qpdmzgy9_50.jpg"},
        "label": {"name": "Rise Records"},
    },
}
TIDAL_HIT = {
    "id": 486406478,
    "title": "Circle With Me",
    "duration": 234,
    "explicit": False,
    "streamStartDate": "2021-09-17T00:00:00.000+0000",
    "mediaMetadata": {"tags": ["LOSSLESS"]},
    "artist": {"name": "Spiritbox"},
    "artists": [{"name": "Spiritbox", "type": "MAIN"}],
    "album": {"title": "Eternal Blue", "cover": "cabed6a2-cd16-4de7-b8b5-2e498a00d35e"},
}


def test_the_same_track_reads_the_same_on_qobuz_and_tidal():
    pages = {
        "qobuz": {"tracks": {"items": [QOBUZ_HIT]}},
        "tidal": {"items": [TIDAL_HIT]},
    }
    qobuz, tidal = (
        SearchResults.from_pages(source, "track", [page]).results[0]
        for source, page in pages.items()
    )

    assert (
        qobuz.summarize() == tidal.summarize() == "Circle With Me by Spiritbox (2021)"
    )
    for summary in (qobuz, tidal):
        details = dict(summary.details)
        assert details["Album"] == "Eternal Blue"
        assert details["Released"] == "2021-09-17"
        assert details["Quality"] == "FLAC 16-bit / 44.1 kHz"
    assert dict(qobuz.details)["Label"] == "Rise Records"  # only Qobuz knows it
    assert qobuz.image_url == "https://static.qobuz.com/t9cn3qpdmzgy9_50.jpg"
    assert tidal.image_url == (
        "https://resources.tidal.com/images/cabed6a2/cd16/4de7/b8b5/2e498a00d35e/"
        "640x640.jpg"
    )


def test_cover_draws_an_edge_inside_a_character():
    # Half blocks could only split a character top from bottom, and blurred a
    # vertical edge through its middle into an average of both sides.
    image = Image.new("RGB", (4, 4), "white")
    for x in (0, 2):  # each character's left half black
        image.paste((0, 0, 0), (x, 0, x + 1, 4))
    buf = io.BytesIO()
    image.save(buf, "PNG")

    (row,) = cover_preview.cover_rows(buf.getvalue(), 1)

    assert row.plain == "▌▌"
    assert {span.style.color.triplet for span in row.spans} == {(0, 0, 0)}


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
    """A copy of the silent FLAC with every tag and picture removed."""
    path = _audio(tmp_path, "flac")
    audio = FLAC(path)
    audio.delete()
    audio.clear_pictures()
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
            tracktotal=40,  # the whole album's; disc 1 has 14
            disctotal=3,
            disc_tracktotals=frozendict({1: 14, 2: 13, 3: 13}),
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
    assert file["genre"] == ["rock", "pop"]  # several values, as artists
    assert file["tracknumber"][0] == "03"
    assert file["discnumber"][0] == "01"
    assert file["copyright"][0] == "© stuff ℗ other stuff"
    assert file["tracktotal"][0] == "14"
    assert file["date"][0] == "1998-02-13"
    assert file["description"][0] == "testdesc"
    assert file["isrc"][0] == "USABC1234567"
    assert "lyrics" not in file  # empty lyrics are left out, not written as ""


@pytest.mark.parametrize("cover", ["too big", "no image"])
async def test_a_cover_that_cannot_be_embedded_does_not_cost_the_tags(
    sample_metadata, flac_path, tmp_path, monkeypatch, cover
):
    # Raised out of tagging, it left the downloaded track untagged and recorded
    # as failed: `repair` then downloaded it again and failed on the same cover.
    cover_path = tmp_path / "cover.jpg"
    if cover == "too big":
        monkeypatch.setattr("streamrip.metadata.tagger.FLAC_MAX_BLOCKSIZE", 100)
        cover_path.write_bytes(TEST_COVER.read_bytes())
    else:
        cover_path.write_text("<html>404</html>")

    await tag_file(flac_path, sample_metadata, cover_path)

    file = FLAC(flac_path)
    assert file["title"][0] == "testtitle"
    assert not file.pictures


async def test_tag_flac_cover(sample_metadata, flac_path):
    await tag_file(flac_path, sample_metadata, TEST_COVER)
    picture = FLAC(flac_path).pictures[0]
    assert picture.data == TEST_COVER.read_bytes()
    # At Mutagen's default of 0 some players take it as "no usable cover".
    assert (picture.width, picture.height, picture.depth) == (1, 1, 24)


async def test_tag_flac_multiple_artists(sample_metadata, flac_path):
    # One "A, B" string shows up in a library as a single artist of that name.
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
    assert tags["\xa9wrt"] == ["testcomposer"]  # once mapped onto the year's atom
    assert tags["\xa9day"] == ["1999"]
    assert tags["trkn"] == [(3, 14)]
    assert tags["disk"] == [(1, 3)]
    assert tags["cprt"] == ["© stuff ℗ other stuff"]
    assert bytes(tags["----:com.apple.iTunes:ISRC"][0]) == b"USABC1234567"
    assert "\xa9lyr" not in tags
    assert len(tags["covr"]) == 1


@pytest.fixture
def opus_path(tmp_path) -> Path:
    if (ffmpeg := find_ffmpeg()) is None:
        pytest.skip("needs ffmpeg")
    path = tmp_path / "track.opus"
    args = ["-f", "lavfi", "-i", "anullsrc=d=0.2", "-c:a", "libopus", str(path)]
    subprocess.run([ffmpeg, "-v", "error", *args], check=True)
    return path


async def test_tag_opus(sample_metadata, opus_path):
    # A file converted to OPUS used to keep ffmpeg's one "A;B" artist.
    path = opus_path
    sample_metadata.artists = ["A", "B"]
    await tag_file(path, sample_metadata, TEST_COVER)
    tags = OggOpus(path)
    assert tags["artist"] == ["A", "B"]
    assert "encoder" not in tags
    assert len(tags["metadata_block_picture"]) == 1


async def test_tag_mp3(sample_metadata, tmp_path):
    path = _audio(tmp_path, "mp3")
    sample_metadata.artists = ["A", "B"]
    await tag_file(path, sample_metadata, None)
    tags = ID3(path, translate=False)  # as written, not upgraded to v2.4
    assert tags.version[:2] == (2, 3)
    assert tags["TIT2"].text == ["testtitle"]
    assert tags["TPE1"].text == ["A/B"]  # ID3v2.3 has no multi-value text frames
    assert tags["TYER"].text == ["1999"]
    assert tags["TRCK"].text == ["3/14"]
    assert tags["TPOS"].text == ["1/3"]
    assert tags["TSRC"].text == ["USABC1234567"]
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


@pytest.mark.parametrize(("ext", "encoder"), [("flac", "encoder"), ("m4a", "\xa9too")])
async def test_retagging_leaves_only_the_new_tags(
    sample_metadata, tmp_path, ext, encoder
):
    # A remux (Deezer's download goes through ffmpeg) leaves its own encoder tag,
    # and FLAC's add_picture appends where MP4 replaces: a re-tagged FLAC used to
    # carry the cover twice.
    path = _audio(tmp_path, ext)
    audio = (FLAC if ext == "flac" else MP4)(path)
    audio[encoder] = ["Lavf60.3.100"]
    audio.save()

    await tag_file(path, sample_metadata, TEST_COVER)
    await tag_file(path, sample_metadata, TEST_COVER)

    assert encoder not in _tags(path)
    covers = FLAC(path).pictures if ext == "flac" else MP4(path).tags["covr"]
    assert len(covers) == 1


async def test_tag_flac_without_a_tag_block(sample_metadata, tmp_path):
    # With no Vorbis comment block mutagen's tags are None: clearing them raised
    # AttributeError and failed every Tidal and Qobuz track in 2.6.7 (issue #91).
    path = _audio(tmp_path, "flac")
    audio = FLAC(path)
    audio.metadata_blocks = [b for b in audio.metadata_blocks if b.code != 4]
    audio.tags = None
    audio.save()

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
                "itunesadvisory": ["1"],
                "replaygain_track_gain": ["-7.08 dB"],
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
                "TXXX:ITUNESADVISORY": ["1"],
                "TXXX:REPLAYGAIN_TRACK_GAIN": ["-7.08 dB"],
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
                "rtng": [1],
                FREE + "replaygain_track_gain": [b"-7.08 dB"],
            },
        ),
    ],
    ids=["flac", "mp3", "m4a"],
)
async def test_label_barcode_release_type_bpm_key_composers_and_replaygain(
    sample_metadata, tmp_path, ext, expected
):
    # Where MusicBrainz Picard puts them, so players and Navidrome read them.
    path = _audio(tmp_path, ext)
    m = sample_metadata
    m.album.info.label, m.album.barcode, m.album.releasetype = "Label", "0123", "ep"
    m.composers, m.composer = ["C1", "C2"], "C1, C2"
    m.bpm, m.key, m.replaygain_gain = 120, "F#m", -7.08
    await tag_file(path, m, None)

    tags = _tags(path)
    assert {name: tags.get(name) for name in expected} == expected


async def test_unknown_or_meaningless_values_are_not_tagged(sample_metadata, flac_path):
    sample_metadata.info.explicit = False  # a clean track gets no advisory
    album = sample_metadata.album
    album.year, album.date = "Unknown", None
    album.tracktotal, album.disctotal = 1, 0  # fallbacks for track 3 of disc 1
    await tag_file(flac_path, sample_metadata, None)
    file = FLAC(flac_path)
    for name in ("itunesadvisory", "bpm", "label", "date", "year", "tracktotal"):
        assert name not in file, name
    assert "disctotal" not in file
    assert file["tracknumber"] == ["03"]
