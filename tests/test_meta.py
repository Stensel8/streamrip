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
    assert m.comment is None
    assert m.compilation is None
    assert (
        m.copyright
        == "© 1977 Warner Records Inc. ℗ 1977 Warner Records Inc. Marketed by Rhino Entertainment Company, A Warner Music Group Company."
    )
    assert m.date == "1977-02-04"
    assert m.description == ""
    assert m.disctotal == 1
    assert m.encoder is None
    assert m.grouping is None
    assert m.lyrics is None
    assert m.purchase_date is None
    assert m.tracktotal == 11


def test_track_metadata_qobuz():
    a = AlbumMetadata.from_qobuz(qobuz_track_resp["album"])
    t = TrackMetadata.from_qobuz(a, qobuz_track_resp)
    info = t.info
    assert info.id == "216020864"
    assert info.quality == 3
    assert info.bit_depth == 24
    assert info.sampling_rate == 96
    assert info.work is None

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
    assert m.albumartist == "A, B"
    assert (m.tracktotal, m.disctotal) == (9, 2)


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
