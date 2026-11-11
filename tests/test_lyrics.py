"""Lyrics from LRCLIB for a track its source sent none for.

The lyrics in these tests are made-up placeholders.
"""

import asyncio
import logging
import socket
from unittest.mock import AsyncMock, MagicMock

import mutagen
import pytest
from aiohttp import web
from test_media import FlacToFlacConverter, _make_track

from streamrip import lyrics
from streamrip.client.audio_match import MatchTrack
from streamrip.config import Config
from streamrip.lyrics import (
    MAX_FAILURES,
    Lrclib,
    LyricsUnavailableError,
    find_lyrics,
    lyrics_of,
)
from streamrip.rip.main import Main

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


# --- what is in a record -----------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "plain", "expected"),
    [
        ({}, False, SYNCED),
        ({}, True, PLAIN),
        ({"plain": ""}, False, SYNCED),
        ({"synced": None}, False, PLAIN),  # no synced ones: plain will do
        ({"plain": None}, True, PLAIN),  # LRC lines made plain
        (
            {
                "plain": "",
                "synced": "[00:01.00][00:30.00] again and again\n[01:02.03]  last line",
            },
            True,
            "again and again\nlast line",
        ),
        ({"plain": "", "synced": ""}, False, None),
        ({"plain": " \n", "synced": None}, True, None),
        ({"instrumental": True}, False, None),
    ],
)
def test_the_lyrics_in_a_record(changes, plain, expected):
    assert lyrics_of(record(**changes), plain) == expected


# --- asking LRCLIB -----------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "plain", "found", "duration"),
    [
        (200.4, False, SYNCED, "200"),  # the length rounded
        (200.0, True, PLAIN, "200"),  # for an mp3
        (None, False, SYNCED, None),
    ],
)
async def test_a_track_is_asked_for_by_artist_title_and_length(
    lrclib, seconds, plain, found, duration
):
    lrclib.on("get", record())

    assert await find_lyrics(WANTED, seconds, plain) == found

    [call] = lrclib.requests("get")
    # The main artist, not "Band, Guest"; no album, which services spell
    # differently.
    query = {"artist_name": "Band", "track_name": "Song"}
    assert call["query"] == (query | {"duration": duration} if duration else query)
    assert lrclib.requests("search") == []
    # It says what is asking, and sends no login.
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["user-agent"].startswith("streamrip/")
    assert "opencommit.eu/Stensel8/streamrip" in headers["user-agent"]
    assert not {"authorization", "cookie", "x-user-auth-token"} & set(headers)


@pytest.mark.parametrize(
    ("results", "found"),
    [
        (
            [
                record(artist="Somebody Else", **WRONG),
                record(track="Song (Live)", **WRONG),
                record(duration=31.0, **WRONG),  # a clip
                record(instrumental=True, **WRONG),
                record(plain="", synced=""),
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
    lrclib.on("get", (404, {"statusCode": 404}))
    lrclib.on("search", results)

    wanted = MatchTrack("Song (feat. Guest)", ["Band"], "Album", 200_000)
    assert await find_lyrics(wanted, 200.0, False) == found

    [call] = lrclib.requests("search")
    assert call["query"] == {"track_name": "Song", "artist_name": "Band"}


async def test_a_miss_is_remembered_and_so_is_a_hit(lrclib):
    lrclib.on("get", (404, {"statusCode": 404}))
    lrclib.on("search", [])

    for _ in range(3):
        assert await find_lyrics(WANTED, 200.0, plain=False) is None
    assert len(lrclib.calls) == 2  # one get, one search, for three asks

    lrclib.calls.clear()
    lrclib.on("get", record())
    other = MatchTrack("Another Song", ["Band"])
    for _ in range(3):
        assert await find_lyrics(other, 200.0, plain=False) == SYNCED
    assert len(lrclib.calls) == 1


# --- when LRCLIB does not answer ---------------------------------------------


async def test_a_failing_lrclib_is_left_alone_after_a_few_tries(lrclib, caplog):
    lrclib.on("get", (500, {"statusCode": 500}))

    with caplog.at_level(logging.WARNING, logger="streamrip"):
        for n in range(MAX_FAILURES + 3):
            with pytest.raises(LyricsUnavailableError):
                await find_lyrics(MatchTrack(f"Song {n}", ["Band"]), 200, False)

    assert len(lrclib.calls) == MAX_FAILURES  # the rest were not even tried
    assert caplog.text.count("LRCLIB does not answer") == 1


async def test_a_lookup_that_waited_for_a_slot_is_not_made_once_lrclib_is_left_alone(
    lrclib,
):
    lrclib.on("get", record())
    client = Lrclib()
    try:
        # Every slot is taken, so lookups have to wait for one...
        for _ in range(lyrics.CONCURRENCY):
            await client._slots.acquire()
        waiting = [
            asyncio.create_task(client.record(MatchTrack(f"Song {n}", ["Band"]), 200))
            for n in range(5)
        ]
        await asyncio.sleep(0)
        # ...and while they wait, enough lookups fail for LRCLIB to be left alone.
        client._failures = MAX_FAILURES
        for _ in range(lyrics.CONCURRENCY):
            client._slots.release()
        results = await asyncio.gather(*waiting, return_exceptions=True)
    finally:
        await client.close()

    assert all(isinstance(result, LyricsUnavailableError) for result in results)
    assert lrclib.calls == []


async def test_a_success_starts_the_count_again(lrclib):
    # Twice: without the reset, the second run of failures reaches the limit.
    for attempt in range(2):
        lrclib.on("get", (500, {}))
        for n in range(MAX_FAILURES - 1):
            wanted = MatchTrack(f"Failing {attempt} {n}", ["Band"])
            with pytest.raises(LyricsUnavailableError):
                await find_lyrics(wanted, 200, False)
        lrclib.on("get", record())
        wanted = MatchTrack(f"Works {attempt}", ["Band"])
        assert await find_lyrics(wanted, 200, False) == SYNCED


async def test_an_unreachable_lrclib_is_unavailable_not_a_crash(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(lyrics, "API", f"http://127.0.0.1:{port}/api")
    monkeypatch.setattr(lyrics, "_lrclib", None)

    try:
        with pytest.raises(LyricsUnavailableError):
            await find_lyrics(WANTED, 200.0, plain=False)
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
    assert caplog.text.count("LRCLIB does not answer") == 1


async def test_closing_is_safe_twice_and_a_new_session_follows(lrclib):
    lrclib.on("get", record())
    await find_lyrics(WANTED, 200.0, plain=False)

    await lyrics.close()
    await lyrics.close()

    assert await find_lyrics(MatchTrack("Another", ["Band"]), 200, False) == SYNCED


async def test_main_closes_the_session_with_the_others(monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr("streamrip.rip.main.lyrics.close", close)
    monkeypatch.setattr(
        "streamrip.utils.ffmpeg_utils.find_ffmpeg", lambda: "/usr/bin/ffmpeg"
    )
    config = Config.defaults()
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False

    async with Main(config):
        close.assert_not_awaited()

    close.assert_awaited_once()


# --- in the download ---------------------------------------------------------


@pytest.fixture
def asked(monkeypatch):
    """Replaces the lookup, and records how it was asked."""
    find = AsyncMock(return_value=SYNCED)
    monkeypatch.setattr("streamrip.media.track.lyrics.find_lyrics", find)
    return find


def lyrics_in(path):
    audio = mutagen.File(path)
    tags = audio.tags
    if hasattr(audio, "pictures"):  # FLAC: a Vorbis comment
        return (tags.get("LYRICS") or [None])[0]
    return (tags.get("\xa9lyr") or [None])[0]


def _from_tidal(track, **get_lyrics):
    track.client = MagicMock(source="tidal")
    track.client.get_lyrics = AsyncMock(**get_lyrics)
    return track


@pytest.mark.parametrize(("extension", "seconds"), [("m4a", 0.32), ("flac", 0.25)])
async def test_lyrics_found_are_in_the_file_tagged(tmp_path, asked, extension, seconds):
    # The synced ones: also in the FLAC of a lossless source.
    await _make_track(tmp_path, extension).rip()

    asked.assert_awaited_once()
    wanted, length, plain, verify_ssl = asked.await_args.args
    assert (wanted.title, wanted.artists, wanted.album) == (
        "Song",
        ["Test Artist"],
        "Test Album",
    )
    # The length of the file, measured.
    assert length == pytest.approx(seconds, abs=0.05)
    assert wanted.duration_ms == pytest.approx(seconds * 1000, abs=50)
    assert (plain, verify_ssl) == (False, True)
    assert lyrics_in(tmp_path / f"Song.{extension}") == SYNCED


@pytest.mark.parametrize(
    ("changes", "plain"),
    [
        ({"download_path": "Song.mp3"}, True),
        ({"download_path": "Song.m4a", "codec": "MP3"}, True),
        ({"download_path": "Song.m4a", "codec": "OPUS"}, False),
    ],
    ids=["an mp3", "converted to mp3", "converted to something else"],
)
async def test_plain_lyrics_are_asked_for_where_the_format_takes_plain(
    tmp_path, asked, changes, plain
):
    track = _make_track(tmp_path, "m4a")
    track.config.session.downloads.save_lrc = True
    await track.preprocess()
    track.download_path = tmp_path / changes["download_path"]  # not even there
    if "codec" in changes:
        track.config.session.conversion.enabled = True
        track.config.session.conversion.codec = changes["codec"]

    await track._look_up_lyrics()

    assert asked.await_args.args[2] is plain
    # Plain lyrics have no timestamps to make an .lrc file worth it.
    assert (tmp_path / "Song.lrc").exists() is not plain


async def test_the_lyrics_of_the_source_come_first(tmp_path, asked):
    track = _from_tidal(_make_track(tmp_path, "m4a"), return_value="from tidal")

    await track.rip()

    track.client.get_lyrics.assert_awaited_once_with("123", False)
    asked.assert_not_awaited()
    assert lyrics_in(tmp_path / "Song.m4a") == "from tidal"


@pytest.mark.parametrize(
    "answer", ["", RuntimeError("401")], ids=["none", "request failed"]
)
async def test_lyrics_the_source_has_not_are_looked_up(tmp_path, asked, answer):
    # A source failing its lyrics costs the track nothing: a 401 from Tidal's
    # used to fail the whole album (#959).
    track = _from_tidal(_make_track(tmp_path, "m4a"), side_effect=[answer])

    await track.rip()

    asked.assert_awaited_once()
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


async def test_a_lookup_that_breaks_costs_the_track_nothing(tmp_path, asked, caplog):
    asked.side_effect = RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="streamrip"):
        await _make_track(tmp_path, "m4a").rip()

    assert (tmp_path / "Song.m4a").exists()
    assert "RuntimeError: boom" in caplog.text


async def test_no_lyrics_found_means_none_in_the_file_and_is_said(
    tmp_path, asked, caplog
):
    asked.return_value = None

    with caplog.at_level(logging.INFO, logger="streamrip"):
        await _make_track(tmp_path, "m4a").rip()

    assert lyrics_in(tmp_path / "Song.m4a") is None
    assert "No lyrics found for 'Song' on LRCLIB" in caplog.text


async def test_the_lookup_comes_before_the_conversion(tmp_path, asked, monkeypatch):
    # Converted files are tagged again from the same metadata.
    monkeypatch.setattr(
        "streamrip.media.track.converter.get", lambda _: FlacToFlacConverter
    )
    track = _make_track(tmp_path, "flac")
    track.config.session.conversion.enabled = True

    await track.rip()

    asked.assert_awaited_once()
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
        _from_tidal(track, return_value=from_source)
    if save_lrc:
        track.config.session.downloads.save_lrc = True

    await track.rip()

    path = tmp_path / "Song.lrc"
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == lrc
