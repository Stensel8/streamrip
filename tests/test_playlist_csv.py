"""CSV files of tracks, as `streamrip playlist export` writes them and every command reads them."""

import pytest
from playlist_support import ISRC_A

from streamrip.sync.csvio import COLUMNS, read_tracks, write_tracks
from streamrip.sync.errors import CsvError
from streamrip.sync.models import Track


def write(tmp_path, text: str, encoding: str = "utf-8"):
    path = tmp_path / "in.csv"
    path.write_text(text, encoding=encoding)
    return path


def test_round_trip(tmp_path):
    tracks = [
        Track(
            "Get Lucky",
            ["Daft Punk", "Pharrell Williams"],
            "Random Access Memories",
            248000,
            isrc=ISRC_A,
            ids={"spotify": "spotify:track:abc"},
        ),
        Track('Comma, "quotes" and ünïcode', ["Tyler, The Creator"]),
    ]
    path = tmp_path / "out" / "tracks.csv"  # the folder is created
    assert write_tracks(path, tracks) == 2
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(COLUMNS)
    assert read_tracks(path) == tracks


def test_a_file_without_a_header_row_is_artist_then_title(tmp_path):
    tracks = read_tracks(write(tmp_path, "Daft Punk,Get Lucky\nBjörk,Jóga\n"))
    assert [(t.artists, t.title) for t in tracks] == [
        (["Daft Punk"], "Get Lucky"),
        (["Björk"], "Jóga"),
    ]


def test_a_list_of_albums_takes_the_album_as_title(tmp_path):
    (t,) = read_tracks(write(tmp_path, "Artist,Album\nDaft Punk,Discovery\n"))
    assert (t.artists, t.title) == (["Daft Punk"], "Discovery")


def test_rows_with_the_wrong_column_count_are_skipped_not_fatal(tmp_path):
    text = "Only one column\nA,B,extra column\n\n"
    assert [t.title for t in read_tracks(write(tmp_path, text))] == ["B"]


def test_column_names_of_other_exporters_are_understood(tmp_path):
    text = (
        "Track URI,Track Name,Album Name,Artist Name(s),Duration (ms)\n"
        "spotify:track:xyz,Song,Album,A;B,201000\n"
    )
    (t,) = read_tracks(write(tmp_path, text, "utf-8-sig"))  # Excel adds a BOM
    assert (t.title, t.artists, t.album, t.duration_ms, t.ids) == (
        "Song",
        ["A", "B"],
        "Album",
        201000,
        {"spotify": "spotify:track:xyz"},
    )


def test_junk_values_are_ignored(tmp_path):
    text = "title,artists,duration_ms,isrc,spotify_uri\nX,Y,not-a-number,ISRC1,https://example.com\n"
    (t,) = read_tracks(write(tmp_path, text))
    assert (t.duration_ms, t.isrc, t.ids) == (None, None, {})


def test_a_file_the_csv_reader_gives_up_on_is_a_csv_error(tmp_path):
    # A quote that is never closed makes the rest one field, and past 128 KiB the reader refuses.
    text = 'title,artists\n"Unclosed,Artist\n' + "Song,Artist\n" * 12_000
    with pytest.raises(CsvError, match=r"in\.csv cannot be read as CSV"):
        read_tracks(write(tmp_path, text))
