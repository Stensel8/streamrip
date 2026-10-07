"""Lyrics from LRCLIB for a track its source sent none for.

The lyrics in these tests are made-up placeholders.
"""

import logging
import socket
from unittest.mock import AsyncMock

import mutagen
import pytest
from aiohttp import web
from test_track import FlacToFlacConverter, _make_track

from streamrip import lyrics
from streamrip.client.audio_match import MatchTrack
from streamrip.config import Config
from streamrip.lyrics import MAX_FAILURES, find_lyrics, lyrics_of
from streamrip.rip.main import Main

PLAIN = "first made-up line\nsecond made-up line"
SYNCED = "[00:01.00] first made-up line\n[00:05.50] second made-up line"
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
async def lrclib(monkeypatch):
    """A running fake LRCLIB, which `find_lyrics` is pointed at."""
    fake = FakeLrclib()
    app = web.Application()
    app.router.add_get("/api/{endpoint}", fake.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    host, port = runner.addresses[0][:2]
    monkeypatch.setattr(lyrics, "API", f"http://{host}:{port}/api")
    monkeypatch.setattr(lyrics, "_lrclib", None)
    yield fake
    await lyrics.close()
    await runner.cleanup()


# --- what is in a record -----------------------------------------------------


def test_synced_lyrics_are_used_unless_plain_ones_are_wanted():
    assert lyrics_of(record(), plain=False) == SYNCED
    assert lyrics_of(record(), plain=True) == PLAIN


@pytest.mark.parametrize(
    ("kinds", "plain", "expected"),
    [
        ({"plain": ""}, False, SYNCED),
        ({"synced": None}, False, PLAIN),  # no synced ones: plain will do
        ({"plain": None}, True, PLAIN),  # LRC lines made plain
        ({"plain": "", "synced": ""}, False, None),
        ({"plain": " \n", "synced": None}, True, None),
    ],
)
def test_whichever_kind_there_is_is_used(kinds, plain, expected):
    assert lyrics_of(record(**kinds), plain) == expected


def test_timestamps_are_taken_off_lrc_lines():
    synced = "[00:01.00][00:30.00] again and again\n[01:02.03]  last line"

    assert lyrics_of(record(plain="", synced=synced), plain=True) == (
        "again and again\nlast line"
    )


def test_an_instrumental_has_no_lyrics():
    assert lyrics_of(record(instrumental=True), plain=False) is None


# --- asking LRCLIB -----------------------------------------------------------


async def test_a_track_is_asked_for_by_artist_title_and_length(lrclib):
    lrclib.on("get", record())

    found = await find_lyrics(WANTED, 200.4, plain=False)

    assert found == SYNCED
    [call] = lrclib.requests("get")
    # The main artist, not "Band, Guest": no album, which services spell
    # differently; the length rounded.
    assert call["query"] == {
        "artist_name": "Band",
        "track_name": "Song",
        "duration": "200",
    }
    assert lrclib.requests("search") == []


async def test_it_says_what_is_asking_and_sends_no_login(lrclib):
    lrclib.on("get", record())

    await find_lyrics(WANTED, 200.0, plain=False)

    headers = {k.lower(): v for k, v in lrclib.calls[0]["headers"].items()}
    assert headers["user-agent"].startswith("streamrip/")
    assert "github.com/Stensel8/streamrip" in headers["user-agent"]
    assert not {"authorization", "cookie", "x-user-auth-token"} & set(headers)


async def test_plain_lyrics_are_given_for_an_mp3(lrclib):
    lrclib.on("get", record())

    assert await find_lyrics(WANTED, 200.0, plain=True) == PLAIN


async def test_without_a_length_none_is_asked_for(lrclib):
    lrclib.on("get", record())

    await find_lyrics(WANTED, None, plain=False)

    assert "duration" not in lrclib.calls[0]["query"]


async def test_when_there_is_no_exact_record_the_search_is_scored(lrclib):
    lrclib.on("get", (404, {"statusCode": 404}))
    wrong = {"plain": "wrong made-up line", "synced": "[00:01.00] wrong made-up line"}
    lrclib.on(
        "search",
        [
            record(artist="Somebody Else", **wrong),
            record(track="Song (Live)", **wrong),
            record(duration=31.0, **wrong),  # a clip
            record(instrumental=True, **wrong),
            record(plain="", synced=""),
            record(track="Song", duration=201.0),
        ],
    )

    found = await find_lyrics(
        MatchTrack("Song (feat. Guest)", ["Band"], "Album", 200_000), 200.0, False
    )

    assert found == SYNCED
    [call] = lrclib.requests("search")
    assert call["query"] == {"track_name": "Song", "artist_name": "Band"}


async def test_a_search_with_nothing_right_in_it_finds_nothing(lrclib):
    lrclib.on("get", (404, {"statusCode": 404}))
    lrclib.on("search", [record(artist="Somebody Else"), record(duration=90.0)])

    assert await find_lyrics(WANTED, 200.0, plain=False) is None


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
            assert (
                await find_lyrics(MatchTrack(f"Song {n}", ["Band"]), 200, False) is None
            )

    assert len(lrclib.calls) == MAX_FAILURES  # the rest were not even tried
    assert caplog.text.count("LRCLIB does not answer") == 1


async def test_a_success_starts_the_count_again(lrclib):
    for n in range(MAX_FAILURES - 1):
        lrclib.on("get", (500, {}))
        await find_lyrics(MatchTrack(f"Failing {n}", ["Band"]), 200, False)
    lrclib.on("get", record())
    assert await find_lyrics(MatchTrack("Works", ["Band"]), 200, False) == SYNCED

    lrclib.calls.clear()
    lrclib.on("get", (500, {}))
    for n in range(MAX_FAILURES - 1):
        await find_lyrics(MatchTrack(f"Failing again {n}", ["Band"]), 200, False)
    lrclib.on("get", record())
    assert await find_lyrics(MatchTrack("Still works", ["Band"]), 200, False) == SYNCED


async def test_an_unreachable_lrclib_is_no_lyrics_not_a_crash(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(lyrics, "API", f"http://127.0.0.1:{port}/api")
    monkeypatch.setattr(lyrics, "_lrclib", None)

    try:
        assert await find_lyrics(WANTED, 200.0, plain=False) is None
    finally:
        await lyrics.close()


async def test_closing_is_safe_twice_and_a_new_session_follows(lrclib):
    lrclib.on("get", record())
    await find_lyrics(WANTED, 200.0, plain=False)

    await lyrics.close()
    await lyrics.close()

    assert await find_lyrics(MatchTrack("Another", ["Band"]), 200, False) == SYNCED


async def test_main_closes_the_session_with_the_others(monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr("streamrip.rip.main.lyrics.close", close)
    monkeypatch.setattr("streamrip.rip.main.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
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


def track_wanting_lyrics(tmp_path, extension="m4a"):
    track = _make_track(str(tmp_path), extension)
    track.config.session.downloads.lyrics_fallback = True
    return track


def lyrics_in(path):
    audio = mutagen.File(path)
    tags = audio.tags
    if hasattr(audio, "pictures"):  # FLAC: a Vorbis comment
        return (tags.get("LYRICS") or [None])[0]
    return (tags.get("\xa9lyr") or [None])[0]


async def test_it_is_off_unless_the_user_turns_it_on(tmp_path, asked):
    track = _make_track(str(tmp_path), "m4a")
    assert track.config.session.downloads.lyrics_fallback is False

    await track.rip()

    asked.assert_not_awaited()
    assert lyrics_in(tmp_path / "Song.m4a") is None


async def test_lyrics_found_are_in_the_file_tagged(tmp_path, asked):
    await track_wanting_lyrics(tmp_path).rip()

    asked.assert_awaited_once()
    wanted, seconds, plain, verify_ssl = asked.await_args.args
    assert (wanted.title, wanted.artists, wanted.album) == (
        "Song",
        ["Test Artist"],
        "Test Album",
    )
    assert seconds == pytest.approx(0.32, abs=0.05)  # the file's, measured
    assert wanted.duration_ms == pytest.approx(320, abs=50)
    assert plain is False  # an m4a takes the synced ones
    assert verify_ssl is True
    assert lyrics_in(tmp_path / "Song.m4a") == SYNCED


async def test_the_flac_of_a_lossless_source_gets_synced_lyrics_too(tmp_path, asked):
    await track_wanting_lyrics(tmp_path, "flac").rip()

    assert lyrics_in(tmp_path / "Song.flac") == SYNCED


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
    track = track_wanting_lyrics(tmp_path)
    track.download_path = str(tmp_path / changes["download_path"])  # not even there
    if "codec" in changes:
        track.config.session.conversion.enabled = True
        track.config.session.conversion.codec = changes["codec"]

    await track._look_up_lyrics()

    assert asked.await_args.args[2] is plain


async def test_the_lyrics_of_the_source_are_left_alone(tmp_path, asked):
    track = track_wanting_lyrics(tmp_path)
    track.meta.lyrics = "the lyrics the source sent"

    await track.rip()

    asked.assert_not_awaited()
    assert lyrics_in(tmp_path / "Song.m4a") == "the lyrics the source sent"


@pytest.mark.parametrize(
    "setting",
    [("downloads", "lyrics", False), ("metadata", "exclude", ["lyrics"])],
    ids=["lyrics off", "lyrics excluded"],
)
async def test_nothing_is_asked_when_lyrics_are_not_wanted(tmp_path, asked, setting):
    track = track_wanting_lyrics(tmp_path)
    section, option, value = setting
    setattr(getattr(track.config.session, section), option, value)

    await track.rip()

    asked.assert_not_awaited()


async def test_a_lookup_that_breaks_costs_the_track_nothing(tmp_path, asked, caplog):
    asked.side_effect = RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="streamrip"):
        await track_wanting_lyrics(tmp_path).rip()

    assert (tmp_path / "Song.m4a").exists()
    assert "Could not look up lyrics for 'Song': RuntimeError: boom" in caplog.text


async def test_no_lyrics_found_means_none_in_the_file(tmp_path, asked):
    asked.return_value = None

    await track_wanting_lyrics(tmp_path).rip()

    assert lyrics_in(tmp_path / "Song.m4a") is None


async def test_the_lookup_comes_before_the_conversion(tmp_path, asked, monkeypatch):
    # Converted files are tagged again from the same metadata.
    monkeypatch.setattr(
        "streamrip.media.track.converter.get", lambda _: FlacToFlacConverter
    )
    track = track_wanting_lyrics(tmp_path, "flac")
    track.config.session.conversion.enabled = True

    await track.rip()

    asked.assert_awaited_once()
    assert lyrics_in(tmp_path / "Song.flac") == SYNCED
