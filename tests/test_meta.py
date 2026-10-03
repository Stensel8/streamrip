import pytest
import json

from streamrip.metadata import AlbumMetadata, TrackMetadata

with open("tests/qobuz_album_resp.json") as f:
    qobuz_album_resp = json.load(f)

with open("tests/qobuz_track_resp.json") as f:
    qobuz_track_resp = json.load(f)


def test_album_metadata_qobuz():
    m = AlbumMetadata.from_qobuz(qobuz_album_resp)
    info = m.info
    assert info.id == "19512572"
    assert info.quality == 3
    assert info.container == "FLAC"
    assert info.label == "Rhino - Warner Records"
    assert info.explicit is False
    assert info.sampling_rate == 96
    assert info.bit_depth == 24
    assert info.booklets is None

    # The edition ("version") is kept in the title so editions do not collide.
    assert m.album == "Rumours (2001 Remaster)"
    assert m.version == "2001 Remaster"
    assert m.albumartist == "Fleetwood Mac"
    assert m.year == "1977"
    assert "Pop" in m.genre
    assert "Rock" in m.genre
    assert not m.covers.empty()

    assert m.albumcomposer == "Various Composers"
    assert m.compilation is None
    assert (
        m.copyright
        == "© 1977 Warner Records Inc. ℗ 1977 Warner Records Inc. Marketed by Rhino Entertainment Company, A Warner Music Group Company."
    )
    assert m.date == "1977-02-04"
    assert m.description == ""
    assert m.disctotal == 1
    assert m.tracktotal == 11


def test_track_metadata_qobuz():
    a = AlbumMetadata.from_qobuz(qobuz_track_resp["album"])
    t = TrackMetadata.from_qobuz(a, qobuz_track_resp)
    info = t.info
    assert info.id == "216020864"
    assert info.explicit is False
    assert t.isrc == "USMRG2384109"

    assert t.title == "Water Tower"
    assert t.album == a
    assert t.artist == "The Mountain Goats"
    assert t.tracknumber == 9
    assert t.discnumber == 1
    assert t.composer == "John Darnielle"


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


def test_tidal_album_with_null_copyright_and_no_date():
    # The fix for a null copyright (upstream #979) only reached the copy of
    # this parser used for single tracks; albums still crashed on it.
    m = AlbumMetadata.from_tidal(_tidal_album(copyright=None, releaseDate=None))
    assert m.copyright == ""
    # No "Unkn" (the first four letters of "Unknown") in tags or folder names.
    assert (m.year, m.date) == ("Unknown", None)


def test_tidal_album_folder_details_match_other_sources():
    m = AlbumMetadata.from_tidal(_tidal_album())
    assert (m.info.container, m.info.bit_depth, m.info.sampling_rate) == (
        "FLAC",
        16,
        44.1,
    )
    assert (m.albumartist, m.albumartists) == ("A, B", ["A", "B"])
    assert (m.tracktotal, m.disctotal) == (9, 2)


def test_tidal_hires_album_folder_shows_the_real_stream_format():
    # The album itself only says LOSSLESS; the client adds what the stream is.
    m = AlbumMetadata.from_tidal(
        _tidal_album(streamQuality={"bitDepth": 24, "sampleRate": 96000})
    )
    assert (m.info.quality, m.info.bit_depth, m.info.sampling_rate) == (3, 24, 96)
    folder = m.format_folder_path(
        "{title} [{container}] [{bit_depth}B-{sampling_rate}kHz]"
    )
    assert folder == "Album [FLAC] [24B-96kHz]"


def test_tidal_hires_album_keeps_fractional_khz():
    m = AlbumMetadata.from_tidal(
        _tidal_album(streamQuality={"bitDepth": 24, "sampleRate": 88200})
    )
    assert m.info.sampling_rate == 88.2


def test_tidal_track_response_gives_its_album():
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
    assert m.info.container == "AAC"


def test_deezer_track_without_album_tracklist():
    track = {
        "explicit_lyrics": True,
        "contributors": [
            {"name": "A", "type": "artist"},
            {"name": "Producer", "type": "producer"},
        ],
        "album": {
            "id": 5,
            "title": "Album",
            **{f"cover_{s}": "u" for s in ("xl", "big", "medium", "small")},
        },
    }
    m = AlbumMetadata.from_track_resp(track, "deezer")
    assert (m.info.id, m.info.container, m.albumartist) == ("5", "FLAC", "A")
    assert m.info.explicit and m.year == "Unknown"


def test_tidal_track_metadata():
    album = AlbumMetadata.from_tidal(_tidal_album())
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
        resp
        | {
            "album": {
                "id": 5,
                "title": "Album",
                **{f"cover_{s}": "u" for s in ("xl", "big", "medium", "small")},
            }
        }
    )
    t = TrackMetadata.from_deezer(album, resp)
    assert (t.info.id, t.info.explicit) == ("8", False)
    assert (t.artist, t.artists, album.albumartist) == ("A, B", ["A", "B"], "A, B")
    assert album.albumartists == ["A", "B"]
    # Without contributors, the main artist.
    del resp["contributors"]
    assert TrackMetadata.from_deezer(album, resp).artists == ["A"]


def test_deezer_album_labels_follow_the_stream_quality():
    resp = {
        "id": 5,
        "title": "Album",
        "release_date": "2020-01-01",
        "artist": {"name": "A"},
        "tracks": [],
        **{f"cover_{s}": "u" for s in ("xl", "big", "medium", "small")},
    }
    fmt = "{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]"

    # Without word from the client, Deezer's lossless tier.
    flac = AlbumMetadata.from_deezer(resp)
    assert (flac.info.container, flac.info.bit_depth, flac.info.sampling_rate) == (
        "FLAC",
        16,
        44.1,
    )
    assert flac.format_folder_path(fmt) == "A - Album (2020) [FLAC] [16B-44.1kHz]"

    # An MP3 tier has no bit depth: its folder name drops that bracket instead
    # of saying "[UnknownB-UnknownkHz]".
    mp3 = AlbumMetadata.from_deezer(resp | {"stream_quality": 1})
    assert (mp3.info.container, mp3.info.bit_depth, mp3.info.sampling_rate) == (
        "MP3",
        None,
        None,
    )
    assert mp3.format_folder_path(fmt) == "A - Album (2020) [MP3]"


def test_soundcloud_track_metadata():
    resp = {
        "id": "123|_original_download",
        "title": " Song",
        "user": {"username": "someone", "avatar_url": "https://a/large.jpg"},
        "artwork_url": None,
        "publisher_metadata": {"explicit": True, "isrc": "X"},
    }
    t = TrackMetadata.from_soundcloud(AlbumMetadata.from_soundcloud(resp), resp)
    assert (t.info.id, t.info.explicit, t.title, t.artist, t.isrc) == (
        "123|_original_download",
        True,
        "Song",
        "someone",
        "X",
    )
    assert (t.tracknumber, t.discnumber) == (1, 1)


def test_qobuz_artists_split_from_performers():
    from streamrip.metadata.util import qobuz_artists

    resp = {
        "performers": "X, Producer - Spiritbox, MainArtist - "
        "Tyler, The Creator, FeaturedArtist - Courtney LaPlante, MainArtist, Vocals"
    }
    assert qobuz_artists(resp) == [
        "Spiritbox",
        "Courtney LaPlante",
        "Tyler, The Creator",
    ]
    assert qobuz_artists({}) == []


def test_qobuz_artists_put_the_main_artist_first():
    # The clean edition of this track lists its feature first; the explicit
    # one lists it last. Both should tag the same artists in the same order.
    from streamrip.metadata.util import qobuz_artists

    clean = {
        "performers": "Spiritbox, Vocals, FeaturedArtist - "
        "Megan Thee Stallion, MainArtist, Vocals - X, Producer"
    }
    assert qobuz_artists(clean) == ["Megan Thee Stallion", "Spiritbox"]


@pytest.mark.parametrize(
    ("fmt", "folder"),
    [
        # A bracket that is only bit depth and sampling rate has nothing to say.
        (
            "{albumartist} - {title} [{container}] [{bit_depth}B-{sampling_rate}kHz]",
            "A - Album [MP3]",
        ),
        ("{albumartist} - {title} [{bit_depth}bit-{sampling_rate}kHz]", "A - Album"),
        # One that also holds the container keeps it: it's still the format.
        (
            "{albumartist} - {title} [{container} {bit_depth}B-{sampling_rate}kHz]",
            "A - Album [MP3 UnknownB-UnknownkHz]",
        ),
    ],
)
def test_lossy_folder_names_drop_only_brackets_that_are_all_quality(fmt, folder):
    resp = {
        "id": 5,
        "title": "Album",
        "artist": {"name": "A"},
        "tracks": [],
        "stream_quality": 1,
        **{f"cover_{s}": "u" for s in ("xl", "big", "medium", "small")},
    }

    assert AlbumMetadata.from_deezer(resp).format_folder_path(fmt) == folder
