"""Lyrics from LRCLIB for a track its source sent none for.

The lyrics in these tests are made-up placeholders.
"""

import logging
import socket
from unittest.mock import AsyncMock, MagicMock

import mutagen
import pytest
from aiohttp import web
from test_media import FlacToFlacConverter, _make_track

from streamrip import lyrics
from streamrip.client.audio_match import MatchTrack
from streamrip.lyrics import (
    MAX_FAILURES,
    LyricsUnavailableError,
    find_lyrics,
    lyrics_of,
)

PLAIN = "first made-up line\nsecond made-up line"
SYNCED = "[00:01.00] first made-up line\n[00:05.50] second made-up line"
WRONG = {"plain": "wrong made-up line", "synced": "[00:01.00] wrong made-up line"}
WANTED = MatchTrack("Song", ["Band", "Guest"], "Album", 200_000)


def record(
    track="Song", artist="Band", duration=200.0, plain=PLAIN, synced=SYNCED, **extra
):
    return {
        "id": 1,
        "trackName": track,
        "artistName": artist,
        "albumName": "Album",
        "duration": duration,
        "instrumental": False,
        "plainLyrics": plain,
        "syncedLyrics": synced,
    } | extra


class FakeLrclib:
    """Answers /api/get and /api/search: a body (200), or a (status, body)."""

    def __init__(self):
        self.answers: dict[str, object] = {}
        self.calls: list[dict] = []

    def on(self, endpoint, answer):
        self.answers[endpoint] = answer

    def requests(self, endpoint):
        return [c for c in self.calls if c["endpoint"] == endpoint]

    async def handle(self, request: web.Request) -> web.Response:
        endpoint = request.match_info["endpoint"]
        self.calls.append(
            {
                "endpoint": endpoint,
                "query": dict(request.query),
                "headers": dict(request.headers),
            }
        )
        answer = self.answers.get(endpoint, (404, {"statusCode": 404}))
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        return web.json_response(body, status=status)


@pytest.fixture
async def lrclib(monkeypatch, serve):
    """A running fake LRCLIB, which `find_lyrics` is pointed at."""
    fake = FakeLrclib()
    base = await serve({"/api/{endpoint}": fake.handle})
    monkeypatch.setattr(lyrics, "API", f"{base}api")
    monkeypatch.setattr(lyrics, "_lrclib", None)
    yield fake
    await lyrics.close()


@pytest.mark.parametrize(
    ("changes", "plain", "expected"),
    [
        ({}, False, SYNCED),
        ({}, True, PLAIN),
        ({"synced": None}, False, PLAIN),  # no synced ones: plain will do
        ({"plain": "", "synced": ""}, False, None),
        ({"instrumental": True}, False, None),
    ],
)
def test_the_lyrics_in_a_record(changes, plain, expected):
    assert lyrics_of(record(**changes), plain) == expected


@pytest.mark.parametrize(("seconds", "duration"), [(200.4, "200"), (None, None)])
async def test_a_track_is_asked_for_by_artist_title_and_length(
    lrclib, seconds, duration
):
    lrclib.on("get", record())

    assert await find_lyrics(WANTED, seconds, False) == SYNCED

    [call] = lrclib.requests("get")
    # The main artist, not "Band, Guest"; no album, which services spell differently.
    query = {"artist_name": "Band", "track_name": "Song"}
    assert call["query"] == (query | {"duration": duration} if duration else query)
    assert lrclib.requests("search") == []
    assert call["headers"]["User-Agent"].startswith("streamrip/")


@pytest.mark.parametrize(
    ("results", "found"),
    [
        (
            [
                record(artist="Somebody Else", **WRONG),
                record(track="Song (Live)", **WRONG),
                record(duration=31.0, **WRONG),  # a clip
                record(track="Song", duration=201.0),
            ],
            SYNCED,
        ),
        ([record(artist="Somebody Else"), record(duration=90.0)], None),
    ],
    ids=["the track among others", "nothing right"],
)
async def test_when_there_is_no_exact_record_the_search_is_scored(
    lrclib, results, found
):
    lrclib.on("search", results)

    wanted = MatchTrack("Song (feat. Guest)", ["Band"], "Album", 200_000)
    assert await find_lyrics(wanted, 200.0, False) == found


async def test_a_failing_lrclib_is_left_alone_after_a_few_tries(lrclib):
    lrclib.on("get", (500, {"statusCode": 500}))

    for n in range(MAX_FAILURES + 3):
        with pytest.raises(LyricsUnavailableError):
            await find_lyrics(MatchTrack(f"Song {n}", ["Band"]), 200, False)

    assert len(lrclib.calls) == MAX_FAILURES  # the rest were not even tried


async def test_an_unreachable_lrclib_is_unavailable_not_a_crash(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(lyrics, "API", f"http://127.0.0.1:{port}/api")
    monkeypatch.setattr(lyrics, "_lrclib", None)

    try:
        with pytest.raises(LyricsUnavailableError):
            await find_lyrics(WANTED, 200.0, False)
    finally:
        await lyrics.close()


async def test_a_track_is_not_said_to_have_no_lyrics_when_lrclib_does_not_answer(
    tmp_path, lrclib, caplog, monkeypatch
):
    monkeypatch.setattr(lyrics, "find_lyrics", find_lyrics)  # the real lookup
    lrclib.on("get", (500, {"statusCode": 500}))

    with caplog.at_level(logging.INFO, logger="streamrip"):
        for n in range(MAX_FAILURES + 2):
            directory = tmp_path / str(n)
            directory.mkdir()
            await _make_track(directory, "m4a").rip()

    assert (tmp_path / "0" / "Song.m4a").exists()  # no track is lost
    assert "No lyrics found" not in caplog.text


@pytest.fixture
def asked(monkeypatch):
    """Replaces the lookup, and records how it was asked."""
    find = AsyncMock(return_value=SYNCED)
    monkeypatch.setattr("streamrip.media.track.lyrics.find_lyrics", find)
    return find


def lyrics_in(path):
    audio = mutagen.File(path)
    key = "LYRICS" if hasattr(audio, "pictures") else "\xa9lyr"  # FLAC or M4A
    return (audio.tags.get(key) or [None])[0]


def from_tidal(track, **get_lyrics):
    track.client = MagicMock(source="tidal")
    track.client.get_lyrics = AsyncMock(**get_lyrics)
    return track


@pytest.mark.parametrize("extension", ["m4a", "flac"])
async def test_lyrics_found_are_in_the_file_tagged(tmp_path, asked, extension):
    await _make_track(tmp_path, extension).rip()

    wanted, length, plain, _ = asked.await_args.args
    assert (wanted.title, wanted.artists) == ("Song", ["Test Artist"])
    assert length > 0  # the length of the file, as synced lyrics only fit one edit
    assert plain is False
    assert lyrics_in(tmp_path / f"Song.{extension}") == SYNCED


async def test_the_lyrics_of_the_source_come_first(tmp_path, asked):
    track = from_tidal(_make_track(tmp_path, "m4a"), return_value="from tidal")

    await track.rip()

    asked.assert_not_awaited()
    assert lyrics_in(tmp_path / "Song.m4a") == "from tidal"


@pytest.mark.parametrize(
    "answer", ["", RuntimeError("401")], ids=["none", "request failed"]
)
async def test_lyrics_the_source_has_not_are_looked_up(tmp_path, asked, answer):
    # A 401 from Tidal's lyrics used to fail the whole album (#959).
    track = from_tidal(_make_track(tmp_path, "m4a"), side_effect=[answer])

    await track.rip()

    assert lyrics_in(tmp_path / "Song.m4a") == SYNCED


@pytest.mark.parametrize(
    "setting",
    [("downloads", "lyrics", False), ("metadata", "exclude", ["lyrics"])],
    ids=["lyrics off", "lyrics excluded"],
)
async def test_nothing_is_asked_when_lyrics_are_not_wanted(tmp_path, asked, setting):
    track = _make_track(tmp_path, "m4a")
    section, option, value = setting
    setattr(getattr(track.config.session, section), option, value)

    await track.rip()

    asked.assert_not_awaited()


@pytest.mark.parametrize(
    "outcome", [{"return_value": None}, {"side_effect": RuntimeError("boom")}]
)
async def test_no_lyrics_found_or_a_lookup_that_breaks_costs_the_track_nothing(
    tmp_path, asked, outcome
):
    asked.configure_mock(**outcome)

    await _make_track(tmp_path, "m4a").rip()

    assert (tmp_path / "Song.m4a").exists()
    assert lyrics_in(tmp_path / "Song.m4a") is None


@pytest.mark.parametrize(("codec", "plain"), [("FLAC", False), ("MP3", True)])
async def test_the_lookup_comes_before_the_conversion(
    tmp_path, asked, monkeypatch, codec, plain
):
    # Converted files are tagged again from the same metadata; MP3 takes plain lyrics.
    monkeypatch.setattr(
        "streamrip.media.track.converter.get", lambda _: FlacToFlacConverter
    )
    track = _make_track(tmp_path, "flac")
    track.config.session.downloads.save_lrc = True
    track.config.session.conversion.enabled = True
    track.config.session.conversion.codec = codec

    await track.rip()

    assert asked.await_args.args[2] is plain
    assert (tmp_path / "Song.lrc").exists() is not plain  # plain has no timestamps
    assert lyrics_in(tmp_path / "Song.flac") == SYNCED


@pytest.mark.parametrize(
    ("save_lrc", "from_source", "lrc"),
    [(False, None, None), (True, None, SYNCED), (True, SYNCED, SYNCED)],
    ids=["off by default", "on", "the source's own lyrics"],
)
async def test_save_lrc_writes_the_synced_lyrics_beside_the_track(
    tmp_path, asked, save_lrc, from_source, lrc
):
    track = _make_track(tmp_path, "m4a")
    if from_source:
        from_tidal(track, return_value=from_source)
    if save_lrc:
        track.config.session.downloads.save_lrc = True

    await track.rip()

    path = tmp_path / "Song.lrc"
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == lrc
