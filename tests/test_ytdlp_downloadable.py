"""YtDlpDownloadable: audio fetched by yt-dlp, kept or re-encoded by ffmpeg.

The "YouTube" here is a local server that serves the audio files of the tests;
yt-dlp reads them like any direct link.
"""

import asyncio
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import mutagen
import pytest
from aiohttp import web

from streamrip.client.downloadable import (
    YtDlpDownloadable,
    _explain_ytdlp_error,
    _fetch_audio,
)
from streamrip.exceptions import FFmpegNotFoundError, NonStreamableError
from streamrip.utils.ffmpeg_utils import find_ffmpeg

TESTS = Path(__file__).parent
needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="needs ffmpeg")


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
    path = str(tmp_path / f"track.{extension}")
    reported: list[int] = []
    await downloadable.download(path, reported.append)
    return path, sum(reported)


@needs_ffmpeg
async def test_an_aac_stream_is_kept_as_it_is(audio_url, tmp_path):
    path, reported = await download(await audio_url("silence.m4a"), tmp_path, "m4a")

    assert Path(path).read_bytes() == (TESTS / "silence.m4a").read_bytes()
    assert reported == os.path.getsize(TESTS / "silence.m4a")


@needs_ffmpeg
async def test_other_audio_is_encoded_as_aac_for_an_m4a(audio_url, tmp_path):
    path, _ = await download(await audio_url("silence.flac"), tmp_path, "m4a")

    audio = mutagen.File(path)
    assert type(audio).__name__ == "MP4"
    assert audio.info.codec == "mp4a.40.2"
    assert audio.info.length == pytest.approx(0.25, abs=0.1)


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
async def test_no_temporary_files_are_left_behind(audio_url, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))

    await download(await audio_url("silence.flac"), tmp_path, "m4a")

    assert list(scratch.iterdir()) == []


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
    that is already gone (it makes the folder again). asyncio.run cancels every
    task a second time as it unwinds, which cancels the one around the thread too.
    """
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
        seen["its folder was still there"] = os.path.isdir(directory)
        os.makedirs(directory, exist_ok=True)  # as yt-dlp does for a .part file
        open(os.path.join(directory, "audio.m4a.part"), "w").close()
        finished.set()
        raise RuntimeError("stopped")

    monkeypatch.setattr("streamrip.client.downloadable._fetch_audio", fetch)
    monkeypatch.setattr(
        "streamrip.client.downloadable.find_ffmpeg", lambda: "/usr/bin/ffmpeg"
    )
    downloadable = YtDlpDownloadable(None, "https://example.com/x", "m4a")
    task = asyncio.create_task(
        downloadable.download(str(tmp_path / "track.m4a"), lambda _: None)
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
    to wait for, so waiting for it would never end.
    """

    class NeverRuns:
        def __init__(self, **_):
            pass

        def submit(self, *_args, **_kwargs):
            return Future()  # pending for ever

        def shutdown(self, **_):
            pass

    monkeypatch.setattr("streamrip.client.downloadable.ThreadPoolExecutor", NeverRuns)
    monkeypatch.setattr(
        "streamrip.client.downloadable.find_ffmpeg", lambda: "/usr/bin/ffmpeg"
    )
    downloadable = YtDlpDownloadable(None, "https://example.com/x", "m4a")
    task = asyncio.create_task(
        downloadable.download(str(tmp_path / "track.m4a"), lambda _: None)
    )
    await asyncio.sleep(0.05)

    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()  # and again, as asyncio.run does
    done, _ = await asyncio.wait([task], timeout=3)

    assert done, "the cancelled download is still waiting for a thread that never began"


def test_no_javascript_runtime_is_enabled_that_yt_dlp_cannot_sandbox(monkeypatch):
    options = {}

    class FakeYoutubeDL:
        def __init__(self, given):
            options.update(given)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, url, download):
            return {"requested_downloads": [{"filepath": "audio.m4a"}]}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYoutubeDL)

    _fetch_audio("https://example.com/x", "/nowhere", True, lambda n: None, None)

    assert set(options["js_runtimes"]) == {"deno", "node", "quickjs"}


async def test_without_ffmpeg_nothing_is_fetched(monkeypatch, tmp_path):
    monkeypatch.setattr("streamrip.client.downloadable.find_ffmpeg", lambda: None)
    downloadable = YtDlpDownloadable(None, "http://127.0.0.1:1/unused.m4a", "m4a")

    with pytest.raises(FFmpegNotFoundError):
        await downloadable.download(str(tmp_path / "track.m4a"), lambda _: None)


@pytest.mark.parametrize("extension", ["flac", "opus", ""])
def test_only_m4a_and_mp3_can_be_asked_for(extension):
    with pytest.raises(ValueError, match="m4a"):
        YtDlpDownloadable(None, "https://example.com/x", extension)


async def test_the_size_is_unknown_until_yt_dlp_is_going():
    downloadable = YtDlpDownloadable(None, "https://example.com/x", "m4a")

    assert await downloadable.size() == 0
    assert downloadable.source == "spotify"
    assert not hasattr(
        downloadable, "id"
    )  # a playlist track would take it for a fallback


def test_a_missing_javascript_runtime_is_explained():
    error = Exception(
        "ERROR: [youtube] x: No supported JavaScript runtime could be found"
    )

    assert "install Deno or Node.js" in _explain_ytdlp_error(error)
    assert not _explain_ytdlp_error(error).startswith("ERROR")


def test_a_bot_check_is_explained():
    error = Exception("Sign in to confirm you're not a bot")

    assert "try again later" in _explain_ytdlp_error(error)


def test_other_errors_are_left_as_they_are():
    assert (
        _explain_ytdlp_error(Exception("ERROR: Video unavailable"))
        == "Video unavailable"
    )
