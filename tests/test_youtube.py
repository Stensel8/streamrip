"""The audio of a Spotify track from YouTube (Music): scoring a result against
the track, picking the result, and yt-dlp fetching it.

The score cases are those of Music-Sync's tests/test_matching.py
(https://github.com/Stensel8/Music-Sync), whose matching this is. The "YouTube"
yt-dlp fetches from is a local server with the audio files of the tests.
"""

import asyncio
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import AsyncMock

import mutagen
import pytest
from aiohttp import web

from streamrip.client.audio_match import MatchTrack, normalize, score, simplify_title
from streamrip.client.downloadable import YtDlpDownloadable, _explain_ytdlp_error
from streamrip.client.ytmusic import YouTubeMusicMatcher, candidates_from, select
from streamrip.exceptions import FFmpegNotFoundError, NonStreamableError
from streamrip.utils.ffmpeg_utils import find_ffmpeg

TESTS = Path(__file__).parent
needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="needs ffmpeg")


def t(title: str, *artists: str, ms: int | None = 200_000) -> MatchTrack:
    return MatchTrack(title, list(artists or ["Artist"]), duration_ms=ms)


# --- scoring -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "simplified", "normalized"),
    [
        ("Song (feat. Someone) - Remastered 2011", "Song", "song"),
        ("S\u00f3ng!", "S\u00f3ng!", "song"),
        ("Song - 2011 Remaster", "Song", "song"),
        ("Song [Deluxe Edition]", "Song", "song"),
    ],
)
def test_titles_lose_their_noise(title, simplified, normalized):
    assert simplify_title(title) == simplified
    assert normalize(title) == normalized


@pytest.mark.parametrize(
    ("wanted", "found", "at_least"),
    [
        (t("Song"), t("Song"), 1.0),
        # Remaster and featuring do not matter, nor an unknown length.
        (t("Song"), t("Song (Remastered 2011)"), 0.95),
        (t("Song"), t("Song - 2011 Remaster"), 0.95),
        (t("Song"), t("Song (feat. X)"), 0.95),
        (t("Song", ms=None), t("Song"), 0.95),
        # Artist spelling is forgiving.
        (t("Song", "The Beatles"), t("Song", "Beatles"), 0.8),
        (t("Song", "Bj\u00f6rk"), t("Song", "Bjork"), 0.8),
        (
            t("Refugee", "Tom Petty and the Heartbreakers"),
            t("Refugee", "Tom Petty"),
            1.0,
        ),
        # How one service writes a track, and how the other the same recording.
        (
            t("Travesuras", "Nicky Jam x J Balvin"),
            t("Travesuras", "Nicky Jam", "J Balvin", ms=206_000),
            0.9,
        ),
        (
            t("Thrift Shop", "Macklemore & Ryan Lewis"),
            t("Thrift Shop (feat. Wanz)", "Macklemore", "Ryan Lewis"),
            0.9,
        ),
        (
            t("Don\u2019t Stop Me Now", "Queen"),
            t("Don't Stop Me Now (Remastered 2011)", "Queen"),
            0.9,
        ),
        (t("Rock 'n' Roll Star", "Oasis"), t("Rock and Roll Star", "Oasis"), 0.9),
        (t("Alone, Pt. II", "Alan Walker"), t("Alone, Part II", "Alan Walker"), 0.9),
        (t("TiK ToK", "Kesha"), t("TiK ToK", "Ke$ha"), 0.9),
        (t("Thunderstruck", "AC/DC"), t("Thunderstruck", "ACDC"), 0.9),
        (t("Levels", "Avicii"), t("Levels (Original Mix)", "Avicii"), 0.9),
        (
            t("Stay (with Justin Bieber)", "The Kid LAROI"),
            t("Stay", "The Kid LAROI", "Justin Bieber"),
            0.9,
        ),
        # The same number, written otherwise.
        (t("Alone, Pt. II"), t("Alone, Part 2"), 0.8),
        (t("Song (Live 1986)"), t("Song (Live)"), 0.8),
        (t("Summer of '69"), t("Summer Of 69 - Remastered 2008"), 0.8),
    ],
)
def test_the_same_recording_matches(wanted, found, at_least):
    assert score(wanted, found) >= at_least


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        (t("Song"), t("Song - Live at Wembley")),
        (t("Song"), t("Song (Acoustic)")),
        (t("Song"), t("Song (Remix)")),
        (
            t("Love Story (Taylor\u2019s Version)", "Taylor Swift"),
            t("Love Story", "Taylor Swift"),
        ),
        (t("Snowfall", "\u00d8neheart"), t("Snowfall (Sped Up)", "\u00d8neheart")),
        (t("Lose Yourself", "Eminem"), t("Lose Yourself (Instrumental)", "Eminem")),
        (
            t("Someone Like You", "Adele"),
            t("Someone Like You (Karaoke Version)", "Adele"),
        ),
        # Another number is another song.
        (t("Song 55"), t("Song 5")),
        (t("Nightcall - Part 1"), t("Nightcall - Part 2")),
    ],
)
def test_another_recording_does_not_match(wanted, found):
    assert score(wanted, found) < 0.8


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        (t("Song", "Artist"), t("Song", "Somebody Else")),
        (t("Jane", "Radio Blazers"), t("Jane", "Roy Blair")),  # only some letters
    ],
)
def test_another_artist_never_matches(wanted, found):
    assert score(wanted, found) == 0.0


def test_a_very_different_length_lowers_the_score():
    assert score(t("Song", ms=400_000), t("Song")) < score(t("Song"), t("Song"))


# --- YouTube Music results -----------------------------------------------------

WANTED = MatchTrack(
    "Get Lucky (feat. Pharrell Williams)",
    ["Daft Punk", "Pharrell Williams"],
    "Random Access Memories",
    248_000,
)
GET_LUCKY = ["Daft Punk", "Pharrell Williams"]


def song(video_id, title, artists, seconds, album=None, explicit=False):
    return {
        "resultType": "song",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": n} for n in artists],
        "album": {"name": album} if album else None,
        "duration_seconds": seconds,
        "isExplicit": explicit,
    }


def video(video_id, title, channel, seconds):
    return {
        "resultType": "video",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": channel}],
        "duration_seconds": seconds,
    }


def test_a_song_becomes_a_candidate_with_its_album_and_length():
    [found] = candidates_from(
        [song("abc", "Get Lucky", ["Daft Punk"], 248, "Random Access Memories", True)]
    )

    assert found.title == "Get Lucky"
    assert found.artists == ["Daft Punk"]
    assert found.album == "Random Access Memories"
    assert found.duration_ms == 248_000
    assert found.explicit is True
    assert found.is_song is True
    assert found.video_id == "abc"


@pytest.mark.parametrize(
    ("title", "channel", "found_title", "artists"),
    [
        (
            "Daft Punk - Get Lucky (Official Video) [HD]",
            "DaftPunkVEVO",
            "Get Lucky",
            ["Daft Punk", "DaftPunk"],  # the title's, then the channel's
        ),
        # A version is no noise.
        (
            "Artist - Song (Official Remix)",
            "Artist - Topic",
            "Song (Official Remix)",
            ["Artist", "Artist"],
        ),
    ],
)
def test_a_video_title_gives_its_artist_and_loses_its_noise(
    title, channel, found_title, artists
):
    [found] = candidates_from([video("v1", title, channel, 248)])

    assert (found.title, found.artists) == (found_title, artists)
    assert found.is_song is False


def test_results_without_an_id_or_title_and_repeats_are_dropped():
    results = [
        {"resultType": "song", "title": "No id", "artists": []},
        {"resultType": "song", "videoId": "x"},
        {"resultType": "artist", "videoId": "y", "title": "An artist"},
        song("same", "Song", ["A"], 100),
        song("same", "Song again", ["A"], 100),
    ]

    assert [c.video_id for c in candidates_from(results)] == ["same"]


@pytest.mark.parametrize(
    ("wanted", "results", "chosen"),
    [
        (
            WANTED,
            [
                song("live", "Get Lucky - Live", ["Daft Punk"], 250),
                song("right", "Get Lucky", GET_LUCKY, 248),
                song("karaoke", "Get Lucky (Karaoke)", ["Backing Band"], 248),
            ],
            "right",
        ),
        (WANTED, [song("other", "Another Song", ["Someone Else"], 248)], None),
        (WANTED, [], None),
        (WANTED, [song("long", "Get Lucky", GET_LUCKY, 369)], None),
        # 15 seconds off: fine for a song, too much for a video with an intro.
        (WANTED, [song("s", "Get Lucky", ["Daft Punk"], 263)], "s"),
        (WANTED, [video("v", "Daft Punk - Get Lucky", "Daft Punk", 263)], None),
        (
            WANTED,
            [
                video("v", "Daft Punk - Get Lucky", "Daft Punk", 248),
                song("s", "Get Lucky", ["Daft Punk"], 248),
            ],
            "s",
        ),
        (
            MatchTrack("Song", ["Artist"], "Album", 200_000, explicit=True),
            [
                song("clean", "Song", ["Artist"], 200, "Album", explicit=False),
                song("dirty", "Song", ["Artist"], 200, "Album", explicit=True),
            ],
            "dirty",
        ),
    ],
    ids=[
        "not another version",
        "nothing like it",
        "nothing at all",
        "another edit",
        "a song 15s off",
        "a video 15s off",
        "a song over an equal video",
        "the same explicit flag",
    ],
)
def test_which_result_is_chosen(wanted, results, chosen):
    match = select(wanted, candidates_from(results))

    assert (match and match.track.video_id) == chosen


class FakeMatcher(YouTubeMusicMatcher):
    """A matcher whose searches answer from a table of (query, filter) -> results."""

    def __init__(self, answers, match_videos=True):
        super().__init__(match_videos)
        self.answers = answers
        self.asked = []

    def _search_blocking(self, query, kind):
        self.asked.append((query, kind))
        answer = self.answers.get((query, kind), [])
        if isinstance(answer, Exception):
            raise answer
        return answer


GOOD = song("good", "Get Lucky", GET_LUCKY, 248)
OFFICIAL = video("vid", "Daft Punk - Get Lucky", "Daft Punk", 248)
ISRC = "GBDUW1300136"
QUERY = "Daft Punk Get Lucky (feat. Pharrell Williams)"


@pytest.mark.parametrize(
    ("answers", "isrc", "match_videos", "chosen", "asked"),
    [
        ({(ISRC, "songs"): [GOOD]}, ISRC, True, "good", [(ISRC, "songs")]),
        (
            {(QUERY, "songs"): [GOOD]},
            ISRC,
            True,
            "good",
            [(ISRC, "songs"), (QUERY, "songs")],
        ),
        ({(QUERY, "songs"): [GOOD]}, None, True, "good", [(QUERY, "songs")]),
        ({(QUERY, "songs"): [GOOD]}, "", True, "good", [(QUERY, "songs")]),
        (
            {(QUERY, "videos"): [OFFICIAL]},
            None,
            True,
            "vid",
            [(QUERY, "songs"), (QUERY, "videos")],
        ),
        ({(QUERY, "videos"): [OFFICIAL]}, None, False, None, [(QUERY, "songs")]),
        # A search that keeps failing is no result, not a crash: tried twice.
        (
            {(QUERY, "songs"): RuntimeError("blocked")},
            None,
            False,
            None,
            [(QUERY, "songs")] * 2,
        ),
    ],
    ids=[
        "isrc",
        "isrc finds nothing",
        "no isrc",
        "empty isrc",
        "videos when no song",
        "no videos by config",
        "search fails",
    ],
)
async def test_how_a_track_is_looked_up(
    monkeypatch, answers, isrc, match_videos, chosen, asked
):
    monkeypatch.setattr("streamrip.client.ytmusic.asyncio.sleep", AsyncMock())
    matcher = FakeMatcher(answers, match_videos)

    match = await matcher.find(WANTED, isrc)

    assert (match and match.track.video_id) == chosen
    assert matcher.asked == asked


# --- yt-dlp --------------------------------------------------------------------


@pytest.fixture
async def audio_url(serve):
    """The url of a file of the tests folder, served locally."""

    async def url(name: str) -> str:
        async def handler(_request):
            return web.FileResponse(TESTS / name)

        return await serve({f"/{name}": handler}) + name

    return url


async def download(url, tmp_path, extension, **options):
    """Download url as `extension`; the path it ends up at, and the progress."""
    downloadable = YtDlpDownloadable(None, url, extension, **options)
    path = tmp_path / f"track.{extension}"
    reported: list[int] = []
    await downloadable.download(path, reported.append)
    return path, sum(reported)


@needs_ffmpeg
async def test_an_aac_stream_is_kept_as_it_is(audio_url, tmp_path):
    path, reported = await download(await audio_url("silence.m4a"), tmp_path, "m4a")

    assert path.read_bytes() == (TESTS / "silence.m4a").read_bytes()
    assert reported == (TESTS / "silence.m4a").stat().st_size


@needs_ffmpeg
async def test_other_audio_is_encoded_as_aac_for_an_m4a(
    audio_url, tmp_path, monkeypatch
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))

    path, _ = await download(await audio_url("silence.flac"), tmp_path, "m4a")

    audio = mutagen.File(path)
    assert type(audio).__name__ == "MP4"
    assert audio.info.codec == "mp4a.40.2"
    assert audio.info.length == pytest.approx(0.25, abs=0.1)
    assert list(scratch.iterdir()) == []  # no temporary files left behind


@needs_ffmpeg
async def test_an_mp3_is_always_encoded_at_the_wanted_bitrate(audio_url, tmp_path):
    path, _ = await download(
        await audio_url("silence.m4a"), tmp_path, "mp3", bitrate=128
    )

    audio = mutagen.File(path)
    assert type(audio).__name__ == "MP3"
    # mutagen works the bitrate out from the size, which is rough for a clip this short.
    assert audio.info.bitrate == pytest.approx(128_000, rel=0.02)


@needs_ffmpeg
async def test_a_link_that_is_gone_is_not_streamable(serve, tmp_path):
    async def gone(_request):
        raise web.HTTPNotFound

    url = await serve({"/gone.m4a": gone}) + "gone.m4a"

    with pytest.raises(NonStreamableError):
        await download(url, tmp_path, "m4a")

    assert not (tmp_path / "track.m4a").exists()


@pytest.mark.parametrize("again", [False, True], ids=["once", "and again"])
async def test_a_cancelled_download_waits_for_its_thread_before_the_folder_goes(
    monkeypatch, tmp_path, again
):
    """yt-dlp's thread cannot be cancelled: left running, it writes in a folder
    that is already gone. asyncio.run cancels every task a second time as it
    unwinds, which cancels the one around the thread too."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    started, finished = threading.Event(), threading.Event()
    seen = {}

    def fetch(url, directory, verify_ssl, report, stop):
        started.set()
        while not stop.wait(0.01):  # "downloading", until it is told to stop
            pass
        time.sleep(0.1)  # a moment of work after it has noticed
        seen["its folder was still there"] = directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True)  # as yt-dlp does for a .part file
        (directory / "audio.m4a.part").touch()
        finished.set()
        raise RuntimeError("stopped")

    monkeypatch.setattr("streamrip.client.downloadable._fetch_audio", fetch)
    monkeypatch.setattr(
        "streamrip.utils.ffmpeg_utils.find_ffmpeg", lambda: "/usr/bin/ffmpeg"
    )
    downloadable = YtDlpDownloadable(None, "https://example.com/x", "m4a")
    task = asyncio.create_task(
        downloadable.download(tmp_path / "track.m4a", lambda _: None)
    )
    await asyncio.to_thread(started.wait, 5)

    task.cancel()
    if again:
        await asyncio.sleep(0.05)
        for other in asyncio.all_tasks() - {asyncio.current_task()}:
            other.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()
    assert seen == {"its folder was still there": True}
    assert list(scratch.iterdir()) == []  # and it is removed after


async def test_a_download_whose_thread_never_began_ends_when_cancelled(
    monkeypatch, tmp_path
):
    """A job that waits for a thread is cancelled before it runs: nothing is left
    to wait for, so waiting for it would never end."""

    class NeverRuns:
        def __init__(self, **_):
            pass

        def submit(self, *_args, **_kwargs):
            return Future()  # pending for ever

        def shutdown(self, **_):
            pass

    monkeypatch.setattr("streamrip.client.downloadable.ThreadPoolExecutor", NeverRuns)
    monkeypatch.setattr(
        "streamrip.utils.ffmpeg_utils.find_ffmpeg", lambda: "/usr/bin/ffmpeg"
    )
    downloadable = YtDlpDownloadable(None, "https://example.com/x", "m4a")
    task = asyncio.create_task(
        downloadable.download(tmp_path / "track.m4a", lambda _: None)
    )
    await asyncio.sleep(0.05)

    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()  # and again, as asyncio.run does
    done, _ = await asyncio.wait([task], timeout=3)

    assert done, "the cancelled download is still waiting for a thread that never began"


async def test_without_ffmpeg_nothing_is_fetched(monkeypatch, tmp_path):
    monkeypatch.setattr("streamrip.utils.ffmpeg_utils.find_ffmpeg", lambda: None)
    downloadable = YtDlpDownloadable(None, "http://127.0.0.1:1/unused.m4a", "m4a")

    with pytest.raises(FFmpegNotFoundError):
        await downloadable.download(tmp_path / "track.m4a", lambda _: None)


@pytest.mark.parametrize("extension", ["flac", "opus", ""])
def test_only_m4a_and_mp3_can_be_asked_for(extension):
    with pytest.raises(ValueError, match="m4a"):
        YtDlpDownloadable(None, "https://example.com/x", extension)


async def test_the_size_is_unknown_until_yt_dlp_is_going():
    downloadable = YtDlpDownloadable(None, "https://example.com/x", "m4a")

    assert await downloadable.size() == 0
    assert downloadable.source == "spotify"
    # A playlist track would take an id for a fallback.
    assert not hasattr(downloadable, "id")


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (
            "ERROR: [youtube] x: No supported JavaScript runtime could be found",
            "install Deno or Node.js",
        ),
        ("Sign in to confirm you're not a bot", "try again later"),
        ("ERROR: Video unavailable", None),
    ],
    ids=["no javascript runtime", "bot check", "anything else"],
)
def test_a_yt_dlp_error_says_what_to_do_about_it(error, hint):
    plain = error.removeprefix("ERROR: ")

    explained = _explain_ytdlp_error(Exception(error))

    if hint is None:
        assert explained == plain
    else:
        assert explained.startswith(plain)
        assert hint in explained
