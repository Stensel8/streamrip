"""A CSV list of tracks: reading it, and finding its tracks on a source."""

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from streamrip.client.audio_match import MatchTrack
from streamrip.config import Config
from streamrip.media.csv_playlist import (
    PendingCsvPlaylist,
    best_result,
    queries,
    read_tracks,
)
from streamrip.metadata import Summary


def write(tmp_path, text, encoding="utf-8", name="list.csv") -> str:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return str(path)


def rows(tracks):
    return [(t.title, t.artists, t.album, t.duration_ms) for t in tracks]


# --- reading -----------------------------------------------------------------


def test_the_csv_of_music_sync_is_read(tmp_path):
    path = write(
        tmp_path,
        "title,artists,album,duration_ms,isrc,spotify_uri,tidal_id\n"
        "Get Lucky,Daft Punk; Pharrell Williams,Random Access Memories,369000,"
        "GBDUW1300136,spotify:track:x,\n"
        "Billie Jean,Michael Jackson,Thriller,293000.0,,,\n",
    )

    assert rows(read_tracks(path)) == [
        (
            "Get Lucky",
            ["Daft Punk", "Pharrell Williams"],
            "Random Access Memories",
            369000,
        ),
        ("Billie Jean", ["Michael Jackson"], "Thriller", 293000),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Exportify: a comma inside a quoted title, and Duration (ms).
        (
            "Track URI,Track Name,Album Name,Artist Name(s),Duration (ms)\n"
            'spotify:track:a,"Hello, Goodbye",Magical Mystery Tour,The Beatles,208000\n',
            [("Hello, Goodbye", ["The Beatles"], "Magical Mystery Tour", 208000)],
        ),
        # TuneMyMusic.
        (
            "Track name,Artist name,Album,Playlist name,Type,ISRC\n"
            "Imagine,John Lennon,Imagine,Mix,Playlist,GBAYE7000001\n",
            [("Imagine", ["John Lennon"], "Imagine", None)],
        ),
        # Excel in many countries: semicolons, and a length as m:ss.
        (
            "Title;Artist;Album;Length\nWaterloo;ABBA;Waterloo;2:45\n",
            [("Waterloo", ["ABBA"], "Waterloo", 165_000)],
        ),
        # Tabs.
        (
            "title\tartist\nSong\tBand\n",
            [("Song", ["Band"], "", None)],
        ),
        # No header row: artist,title.
        (
            "Queen,Bohemian Rhapsody\nABBA,Dancing Queen\n",
            [
                ("Bohemian Rhapsody", ["Queen"], "", None),
                ("Dancing Queen", ["ABBA"], "", None),
            ],
        ),
    ],
    ids=["exportify", "tunemymusic", "semicolons", "tabs", "no header"],
)
def test_other_layouts_are_understood(tmp_path, text, expected):
    assert rows(read_tracks(write(tmp_path, text))) == expected


def test_a_byte_order_mark_from_excel_does_not_hide_the_header(tmp_path):
    path = write(tmp_path, "Title;Artist\nSong;Band\n", encoding="utf-8-sig")

    assert rows(read_tracks(path)) == [("Song", ["Band"], "", None)]


def test_blank_rows_and_rows_without_a_title_are_skipped(tmp_path, caplog):
    path = write(tmp_path, "title,artists\n\n,Nobody\nSong,Somebody\n , \n")

    with caplog.at_level(logging.WARNING, logger="streamrip"):
        tracks = read_tracks(path)

    assert rows(tracks) == [("Song", ["Somebody"], "", None)]
    assert "skipped 1 row(s) without a title" in caplog.text


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        ("duration_ms\n123456.0\n", 123456),
        ("duration_ms\nnot a number\n", None),
        ("duration\n3:45\n", 225_000),
        ("length\n1:02:03\n", 3_723_000),
        # A bare number could be seconds or milliseconds: better to say nothing.
        ("duration\n225\n", None),
        ("duration\n225000\n", None),
    ],
)
def test_a_length_is_only_taken_when_its_unit_is_clear(tmp_path, duration, expected):
    header, value = duration.split("\n")[:2]
    path = write(tmp_path, f"title,{header}\nSong,{value}\n")

    assert read_tracks(path)[0].duration_ms == expected


def test_an_empty_file_or_one_with_only_a_header_has_no_tracks(tmp_path):
    assert read_tracks(write(tmp_path, "")) == []
    assert read_tracks(write(tmp_path, "title,artists\n")) == []


def test_a_file_that_is_not_utf8_is_refused_with_advice(tmp_path):
    path = write(tmp_path, "title,artists\nCaf\u00e9,Band\n", encoding="latin-1")

    with pytest.raises(ValueError, match="UTF-8"):
        read_tracks(path)


# --- choosing a result -------------------------------------------------------

WANTED = MatchTrack(
    "Get Lucky (feat. Pharrell Williams)",
    ["Daft Punk", "Pharrell Williams"],
    "Random Access Memories",
    369_000,
)


def result(name, artist, album="", length=None, id="1"):
    details = [("Album", album), ("Length", length)]
    return Summary("track", id, name, artist, details=[d for d in details if d[1]])


def test_the_result_that_is_the_track_is_chosen():
    results = [
        result("Get Lucky - Live", "Daft Punk", id="live"),
        result("Get Lucky", "Daft Punk, Pharrell Williams", id="right"),
        result("Another Song", "Daft Punk", id="other"),
    ]

    hit = best_result(WANTED, results)

    assert hit is not None
    assert hit[0].id == "right"
    assert hit[1] >= 0.9


def test_of_two_edits_the_one_with_the_length_wins():
    radio_edit = result("Get Lucky", "Daft Punk", "RAM", "4:08", id="edit")
    album = result("Get Lucky", "Daft Punk", "Random Access Memories", "6:09", id="lp")

    hit = best_result(WANTED, [radio_edit, album])

    assert hit is not None
    assert hit[0].id == "lp"


@pytest.mark.parametrize(
    "results",
    [
        [],
        [result("Get Lucky", "Somebody Else")],
        [result("Get Lucky (Karaoke Version)", "Daft Punk")],
        [result("Something Else Entirely", "Daft Punk")],
    ],
    ids=["nothing", "another artist", "another version", "another song"],
)
def test_nothing_is_chosen_when_no_result_is_the_track(results):
    assert best_result(WANTED, results) is None


def test_the_title_is_searched_without_its_brackets_first():
    assert queries(WANTED) == [
        "Daft Punk Get Lucky",
        "Daft Punk Get Lucky (feat. Pharrell Williams)",
    ]


def test_a_plain_title_is_searched_once_and_no_artist_is_no_gap():
    assert queries(MatchTrack("Song", ["Band"])) == ["Band Song"]
    assert queries(MatchTrack("Song")) == ["Song"]


# --- finding the tracks ------------------------------------------------------


class Catalogue:
    """A source that finds tracks by a table of query -> results."""

    def __init__(self, source, table, fail=()):
        self.source = source
        self.table = table
        self.fail = fail
        self.asked = []

    async def search(self, media_type, query, limit=50, offset=0):
        self.asked.append((media_type, query, limit))
        if query in self.fail:
            raise ConnectionError("down")
        return [{"items": self.table.get(query, [])}]


def item(id, title, artist, album="Album", seconds=200):
    return {
        "id": id,
        "title": title,
        "artists": [{"name": artist}],
        "album": {"title": album},
        "duration": seconds,
    }


def pending(tmp_path, tracks, client, fallback=None, name="Mix"):
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path)
    config.session.cli.progress_bars = False
    return PendingCsvPlaylist(name, tracks, client, fallback, config, MagicMock())


ONE = MatchTrack("Song One", ["Band"], duration_ms=200_000)
TWO = MatchTrack("Song Two", ["Band"], duration_ms=200_000)
THREE = MatchTrack("Song Three", ["Band"], duration_ms=200_000)


async def test_the_tracks_found_become_a_playlist_in_a_folder_of_the_lists_name(
    tmp_path,
):
    tidal = Catalogue(
        "tidal",
        {
            "Band Song One": [item(11, "Song One", "Band")],
            "Band Song Three": [item(33, "Song Three", "Band")],
        },
    )

    playlist = await pending(tmp_path, [ONE, TWO, THREE], tidal).resolve()

    assert playlist is not None
    assert playlist.name == "Mix"
    assert [(t.id, t.position, t.total) for t in playlist.tracks] == [
        ("11", 1, 3),
        ("33", 3, 3),  # the position in the list, also where one is missing
    ]
    assert {t.folder for t in playlist.tracks} == {str(tmp_path / "Mix")}
    assert all(t.client is tidal for t in playlist.tracks)
    assert ("track", "Band Song One", 5) in tidal.asked  # five results are looked at


async def test_a_track_the_source_lacks_is_looked_for_on_the_fallback(tmp_path):
    tidal = Catalogue("tidal", {"Band Song One": [item(11, "Song One", "Band")]})
    spotify = Catalogue("spotify", {"Band Song Two": [item(22, "Song Two", "Band")]})

    playlist = await pending(tmp_path, [ONE, TWO], tidal, spotify).resolve()

    assert [(t.id, t.client.source) for t in playlist.tracks] == [
        ("11", "tidal"),
        ("22", "spotify"),
    ]


async def test_a_track_that_is_nowhere_is_reported_and_skipped(tmp_path, caplog):
    tidal = Catalogue("tidal", {"Band Song One": [item(11, "Song One", "Band")]})

    with caplog.at_level(logging.INFO, logger="streamrip"):
        playlist = await pending(tmp_path, [ONE, TWO], tidal).resolve()

    assert [t.id for t in playlist.tracks] == ["11"]
    assert "Not found: Band - Song Two" in caplog.text
    assert "Mix: found 1 of 2 tracks" in caplog.text


async def test_a_wrong_result_is_not_taken_for_the_track(tmp_path):
    tidal = Catalogue("tidal", {"Band Song One": [item(99, "Song One (Live)", "Band")]})

    assert await pending(tmp_path, [ONE], tidal).resolve() is None


async def test_when_nothing_is_found_there_is_no_playlist(tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger="streamrip"):
        playlist = await pending(tmp_path, [ONE, TWO], Catalogue("tidal", {})).resolve()

    assert playlist is None
    assert "None of the tracks of Mix was found" in caplog.text


async def test_a_track_listed_twice_is_downloaded_once(tmp_path):
    tidal = Catalogue(
        "tidal",
        {
            "Band Song One": [item(11, "Song One", "Band")],
            "Band Song Two": [item(22, "Song Two", "Band")],
        },
    )

    playlist = await pending(tmp_path, [ONE, TWO, ONE], tidal).resolve()

    assert [(t.id, t.position) for t in playlist.tracks] == [("11", 1), ("22", 2)]


async def test_a_search_that_fails_does_not_end_the_list(tmp_path, caplog):
    tidal = Catalogue(
        "tidal",
        {"Band Song Two": [item(22, "Song Two", "Band")]},
        fail={"Band Song One"},
    )

    with caplog.at_level(logging.WARNING, logger="streamrip"):
        playlist = await pending(tmp_path, [ONE, TWO], tidal).resolve()

    assert [t.id for t in playlist.tracks] == ["22"]
    assert "Searching tidal for 'Band Song One' failed: down" in caplog.text


async def test_the_full_title_is_tried_when_the_short_one_finds_nothing(tmp_path):
    wanted = MatchTrack("Song (feat. Guest)", ["Band"], duration_ms=200_000)
    tidal = Catalogue(
        "tidal",
        {"Band Song (feat. Guest)": [item(5, "Song (feat. Guest)", "Band")]},
    )

    playlist = await pending(tmp_path, [wanted], tidal).resolve()

    assert [q for _, q, _ in tidal.asked] == ["Band Song", "Band Song (feat. Guest)"]
    assert [t.id for t in playlist.tracks] == ["5"]


@pytest.mark.parametrize("name", [".", "..", ""])
async def test_a_name_that_is_no_folder_is_refused(tmp_path, caplog, name):
    tidal = Catalogue("tidal", {"Band Song One": [item(11, "Song One", "Band")]})

    with caplog.at_level(logging.ERROR, logger="streamrip"):
        playlist = await pending(tmp_path, [ONE], tidal, name=name).resolve()

    assert playlist is None
    assert tidal.asked == []
    assert "Error creating playlist" in caplog.text


async def test_the_progress_shows_what_is_found(tmp_path, monkeypatch):
    spinner = MagicMock()

    @contextmanager
    def status(text, **_):
        yield spinner

    monkeypatch.setattr("streamrip.media.csv_playlist.console.status", status)
    tidal = Catalogue("tidal", {"Band Song One": [item(11, "Song One", "Band")]})
    config = Config.defaults()
    config.session.downloads.folder = str(tmp_path)
    config.session.cli.progress_bars = True

    await PendingCsvPlaylist(
        "Mix", [ONE, TWO], tidal, None, config, MagicMock()
    ).resolve()

    shown = [call.args[0].plain for call in spinner.update.call_args_list]
    assert shown[-1] == "Searching Tidal for the tracks (1 found, 1 not found, 2 total)"
