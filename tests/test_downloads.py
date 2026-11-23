"""Downloads that resume, the ffmpeg they need, and the conversions it does."""

import hashlib
import http.server
import shutil
import sys
import threading
import time
import types
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import mutagen
import pytest
import requests
from click.testing import CliRunner
from multidict import CIMultiDict

from streamrip import converter
from streamrip.client import new_session
from streamrip.client.downloadable import (
    BasicDownloadable,
    _content_range,
    fast_async_download,
)
from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import (
    ConversionError,
    FFmpegNotFoundError,
    IncompleteDownloadError,
    TrackDownloadFailedError,
)
from streamrip.filepath_utils import fit_filename
from streamrip.media.track import MAX_DOWNLOAD_ATTEMPTS, Track
from streamrip.rip.cli import rip
from streamrip.rip.main import Main
from streamrip.utils.ffmpeg_utils import find_ffmpeg

TOTAL = 1_000_000
PAYLOAD = bytes((i * 31 + 7) % 256 for i in range(TOTAL))
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).digest()
SILENCE = Path(__file__).with_name("silence.flac")

needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="needs ffmpeg")


def _ignore(_):
    pass


# --- resuming a download ------------------------------------------------------


@pytest.fixture
def server():
    """Start a local HTTP server: `url = server(answer)`, where answer(handler)
    replies to each GET."""
    servers = []

    def start(answer) -> str:
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                answer(self)

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}/track.flac"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()  # or its listening socket stays open


def _reply(handler, status, body=b"", headers=None):
    handler.send_response(status)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _interrupted(on_range):
    """Drop the first response halfway (the IncompleteRead seen on large
    tracks); answer a Range request with on_range(handler, start)."""

    def answer(handler):
        if (rng := handler.headers.get("Range")) is None:
            _reply(handler, 200, PAYLOAD[: TOTAL // 2], {"Content-Length": str(TOTAL)})
            handler.wfile.flush()
            handler.close_connection = True
            handler.connection.close()
        else:
            on_range(handler, int(rng.removeprefix("bytes=").split("-")[0]))

    return answer


def _partial(shift=0, total=TOTAL):
    """A 206 from `shift` bytes off the asked start, saying the file is `total`."""

    def on_range(handler, start):
        start += shift
        content_range = f"bytes {start}-{TOTAL - 1}/{total}"
        headers = {"Content-Range": content_range, "Content-Length": str(TOTAL - start)}
        _reply(handler, 206, PAYLOAD[start:], headers)

    return on_range


def _whole(handler, _start):
    _reply(handler, 200, PAYLOAD, {"Content-Length": str(TOTAL)})


def _unknown_total_cut_short(handler, start):
    # No Content-Length: delimited by the connection closing, so urllib3
    # cannot tell it ended early.
    headers = {"Content-Range": f"bytes {start}-{TOTAL - 1}/*"}
    _reply(handler, 206, PAYLOAD[start : start + 1000], headers)
    handler.wfile.flush()
    handler.close_connection = True


@pytest.mark.parametrize(
    ("on_range", "error"),
    [
        (_partial(), None),
        (_whole, None),  # Range ignored: overwritten, not appended to
        # Appended, these would leave a corrupt file of plausible size: urllib3
        # only checks a 206 body against its own length.
        (_partial(shift=-1000), "resume at byte"),
        (_partial(total=TOTAL + 5), "announced"),
        (_unknown_total_cut_short, "announced"),
    ],
    ids=["206", "200", "wrong offset", "wrong total", "unknown total, cut short"],
)
async def test_an_interrupted_download_resumes_or_starts_over(
    server, tmp_path, on_range, error
):
    url = server(_interrupted(on_range))
    path = tmp_path / "track.flac"
    with pytest.raises(requests.RequestException):  # the dropped first response
        await fast_async_download(path, url, {}, _ignore)
    assert path.exists()

    if error is None:
        await fast_async_download(path, url, {}, _ignore, resume=True)
        assert hashlib.sha256(path.read_bytes()).digest() == PAYLOAD_SHA
    else:
        with pytest.raises(IncompleteDownloadError, match=error):
            await fast_async_download(path, url, {}, _ignore, resume=True)
        # Removed, so the next attempt starts from scratch instead of resuming
        # onto the same mismatch.
        assert not path.exists()


async def test_what_arrived_before_a_break_is_kept_and_resumed(server, tmp_path):
    """#122: Qobuz's CDN hung up after the first byte of a track, every time.
    That byte was lost with the chunk it was read into, so the part file
    stayed empty and every retry was the same request from the start."""
    asked = []

    def answer(handler):
        asked.append((handler.headers.get("Range"), handler.headers["Accept-Encoding"]))
        if handler.headers.get("Range") is None:
            _reply(handler, 200, PAYLOAD[:1], {"Content-Length": str(TOTAL)})
            handler.wfile.flush()
            handler.close_connection = True
            handler.connection.close()
        else:
            _partial()(handler, 1)

    url = server(answer)
    path = tmp_path / "track.part.flac"
    with pytest.raises(requests.exceptions.ChunkedEncodingError, match="1 bytes read"):
        await fast_async_download(path, url, {}, _ignore)
    assert path.read_bytes() == PAYLOAD[:1]

    await fast_async_download(path, url, {}, _ignore, resume=True)
    assert hashlib.sha256(path.read_bytes()).digest() == PAYLOAD_SHA
    # Never compressed on the way: the Range offset is one in the file itself.
    assert asked == [(None, "identity"), ("bytes=1-", "identity")]


@pytest.mark.parametrize(("size", "kept"), [(1000, True), (1200, False)])
async def test_a_416_keeps_only_a_partial_file_of_the_announced_length(
    server, tmp_path, size, kept
):
    """A 416 used to mean "complete" whatever the local size."""
    headers = {"Content-Range": "bytes */1000", "Content-Length": "0"}
    url = server(lambda handler: _reply(handler, 416, headers=headers))
    path = tmp_path / "track.flac"
    path.write_bytes(b"x" * size)

    if kept:
        await fast_async_download(path, url, {}, _ignore, resume=True)
        assert path.read_bytes() == b"x" * 1000
    else:
        with pytest.raises(IncompleteDownloadError, match="416"):
            await fast_async_download(path, url, {}, _ignore, resume=True)
        assert not path.exists()


async def test_a_first_attempt_never_appends_to_a_file_already_there(server, tmp_path):
    """A re-download over a tagged track or a downscaled cover.jpg would
    otherwise be "resumed" into a corrupt file."""

    def answer(handler):
        assert handler.headers.get("Range") is None
        _reply(handler, 200, PAYLOAD[:50_000], {"Content-Length": "50000"})

    path = tmp_path / "cover.jpg"
    path.write_bytes(b"old, smaller file")
    await fast_async_download(path, server(answer), {}, _ignore)
    assert path.read_bytes() == PAYLOAD[:50_000]


@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_a_file_is_fetched_with_the_certificate_check_of_its_session(
    monkeypatch, tmp_path, verify_ssl
):
    # --no-ssl-verify reached the API requests and not the downloads themselves:
    # behind a proxy that inspects TLS, everything resolved and every file failed.
    asked = {}

    def get(url, **kwargs):
        asked.update(kwargs)
        raise requests.ConnectionError("far enough")

    monkeypatch.setattr("streamrip.client.downloadable.requests.get", get)
    async with new_session(verify_ssl=verify_ssl) as session:
        file = BasicDownloadable(session, "https://cdn.test/file", "flac")
        with pytest.raises(requests.ConnectionError):
            await file.download(tmp_path / "file.flac", lambda _: None)
    assert asked["verify"] is verify_ssl


async def test_multidict_headers_are_accepted(server, tmp_path):
    """aiohttp's istr header keys used to make requests refuse the call (#941)."""
    seen = {}

    def answer(handler):
        seen["ua"] = handler.headers.get("User-Agent")
        _reply(handler, 200, b"ok", {"Content-Length": "2"})

    headers = CIMultiDict({"User-Agent": "streamrip-test"})
    await fast_async_download(tmp_path / "f", server(answer), headers, _ignore)
    assert seen["ua"] == "streamrip-test"


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("bytes 500-999/1000", (500, 999, 1000)),
        ("Bytes 500-999/1000", (500, 999, 1000)),  # units are case-insensitive
        ("bytes 0-0/*", (0, 0, None)),
        ("bytes */1000", (None, None, 1000)),  # the 416 form
        (None, (None, None, None)),
        ("garbage", (None, None, None)),
    ],
)
def test_content_range_parsing(value, parsed):
    assert _content_range(value) == parsed


# --- ffmpeg -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("on_path", "imageio", "found"),
    [
        ("/usr/bin/ffmpeg", None, "/usr/bin/ffmpeg"),
        (None, "/fake/imageio_ffmpeg/ffmpeg", "/fake/imageio_ffmpeg/ffmpeg"),
        (None, None, None),
    ],
)
def test_find_ffmpeg_prefers_the_one_on_path(on_path, imageio, found):
    module = None
    if imageio:
        module = types.ModuleType("imageio_ffmpeg")
        module.get_ffmpeg_exe = lambda: imageio
    find_ffmpeg.cache_clear()
    try:
        with (
            patch("shutil.which", side_effect={"ffmpeg": on_path}.get),
            patch.dict(sys.modules, {"imageio_ffmpeg": module}),
        ):
            assert find_ffmpeg() == found
    finally:
        # It caches: a fake path (or None) left there made every later test
        # that really needs ffmpeg fail.
        find_ffmpeg.cache_clear()


@pytest.mark.parametrize("ffmpeg", ["/usr/bin/ffmpeg", None])
async def test_main_starts_only_with_ffmpeg(ffmpeg):
    config = Config.defaults()
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    main = Main(config)
    refused = pytest.raises(FFmpegNotFoundError, match="No ffmpeg installation")
    with (
        patch("streamrip.utils.ffmpeg_utils.find_ffmpeg", return_value=ffmpeg),
        nullcontext() if ffmpeg else refused,
    ):
        async with main as entered:
            assert entered is main


@pytest.mark.login
@pytest.mark.parametrize("command", ["url", "file", "id", "lastfm", "search", "csv"])
def test_every_login_command_checks_ffmpeg_first(tmp_path, command):
    cfg = tmp_path / "config.toml"
    set_user_defaults(cfg)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://tidal.com/album/1\n")
    tracks = tmp_path / "tracks.csv"
    tracks.write_text("title,artists\nSong,Band\n")
    args = {
        "url": ["url", "https://tidal.com/album/1"],
        "file": ["file", str(urls)],
        "id": ["id", "qobuz", "album", "1"],
        "lastfm": ["lastfm", "https://www.last.fm/user/x/playlists/1"],
        "search": ["search", "deezer", "track", "x"],
        "csv": ["csv", str(tracks), "--source", "tidal"],
    }[command]
    with (
        patch("streamrip.utils.ffmpeg_utils.find_ffmpeg", return_value=None),
        patch.object(Main, "get_logged_in_client", AsyncMock()) as login,
        # The console may hold on to the stdout it was created with, so check
        # what is printed instead of what CliRunner captures.
        patch("streamrip.rip.cli.console") as console,
    ):
        result = CliRunner().invoke(rip, ["--config-path", str(cfg), *args])
    assert result.exit_code == 1
    printed = "".join(str(call.args[0]) for call in console.print.call_args_list)
    assert "No ffmpeg installation found" in printed
    assert "run streamrip's installer again" in printed
    login.assert_not_called()


async def test_missing_ffmpeg_is_not_retried():
    track = MagicMock()
    track._skip_lossy_duplicate = False
    track.client = None  # the fallback needs a client; this one has none
    track._fallback_to_youtube = lambda e: False  # it is a real Track's method
    track.config.session.downloads.max_connections = 6
    track.downloadable.size = AsyncMock(return_value=0)
    track.downloadable.download = AsyncMock(side_effect=FFmpegNotFoundError("no"))
    track.downloadable.source = "tidal"
    track.downloadable.resolved_at = time.time()
    track.config.session.cli.progress_bars = False
    track.is_single = False
    track.download_path = Path("/nonexistent/track.flac")
    track.meta.title = "T"
    track.meta.tracknumber = 1
    track.meta.info.id = "1"
    track._download_attempts = partial(Track._download_attempts, track)
    track._lower_quality = partial(Track._lower_quality, track)
    with (
        patch("streamrip.media.track.format_quality", return_value="FLAC"),
        patch("streamrip.media.track.asyncio.sleep", AsyncMock()) as sleep,
        pytest.raises(TrackDownloadFailedError),
    ):
        await Track.download(track)
    assert MAX_DOWNLOAD_ATTEMPTS > 1  # or one attempt proves nothing
    assert track.downloadable.download.await_count == 1
    sleep.assert_not_called()


def _failing_track(tmp_path, served: int, configured: int = 4, works_at: int = 2):
    """A Track whose downloads fail at every quality above `works_at`."""
    track = object.__new__(Track)
    track._skip_lossy_duplicate = False
    track.config = MagicMock()
    track.config.session.downloads.max_connections = 1
    track.config.session.cli.progress_bars = False
    track.config.session.qobuz.quality = configured
    track.download_path = tmp_path / "Song.flac"
    track.meta = MagicMock()
    track.meta.title = "One Love"
    track.meta.info.id = "6763169"
    asked = []

    def file_at(tier: int):
        """What Qobuz serves for `tier`: its best up to it, 24/96 at most."""
        file = MagicMock()
        file.quality, file.extension, file.url_expires = min(tier, served), "flac", True
        file.resolved_at = time.time()
        file.size = AsyncMock(return_value=0)

        async def download(path, _callback):
            asked.append(file.quality)
            if file.quality > works_at:
                raise requests.exceptions.ChunkedEncodingError("IncompleteRead")
            path.write_bytes(b"fLaC")

        file.download = download
        return file

    track.client = MagicMock(source="qobuz")
    track.client.get_downloadable = AsyncMock(
        side_effect=lambda _id, tier: file_at(tier)
    )
    track.downloadable = file_at(configured)
    return track, asked


@pytest.mark.parametrize(
    ("served", "tried"),
    [
        # Asked for 4, Qobuz serves 24/96: straight to 16/44.1, since asking
        # for 3 would only get the very file that failed.
        (3, [3, 2]),
        (4, [4, 3, 2]),  # 24/192, 24/96, 16/44.1
    ],
)
async def test_a_track_that_keeps_failing_steps_down_one_lossless_quality(
    tmp_path, caplog, served, tried
):
    """#122."""
    track, asked = _failing_track(tmp_path, served)
    with (
        patch("streamrip.media.track.format_quality", return_value="FLAC"),
        patch("streamrip.media.track.asyncio.sleep", AsyncMock()),
    ):
        await track.download()
    assert track.download_path.read_bytes() == b"fLaC"
    expected = [t for t in tried[:-1] for _ in range(MAX_DOWNLOAD_ATTEMPTS)]
    assert asked == [*expected, 2]
    assert "downloading it at 16 bit, 44.1 kHz instead" in caplog.text


async def test_a_failing_track_never_steps_down_to_a_lossy_quality(tmp_path):
    track, asked = _failing_track(tmp_path, served=3, works_at=1)  # MP3 only
    with (
        patch("streamrip.media.track.format_quality", return_value="FLAC"),
        patch("streamrip.media.track.asyncio.sleep", AsyncMock()),
        pytest.raises(TrackDownloadFailedError),
    ):
        await track.download()
    assert set(asked) == {3, 2}  # 16/44.1 FLAC at the lowest, never MP3 (1)
    assert not track.download_path.exists()


# --- conversion ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("lossless", "ffmpeg_arg"), [(False, "-b:a 160k"), (True, None)]
)
async def test_the_lossy_bitrate_reaches_only_a_lossy_converter(
    monkeypatch, lossless, ffmpeg_arg
):
    made = []

    class Engine:
        @classmethod
        def get_quality_arg(cls, rate):
            assert not cls.lossless, "lossless converters should not use lossy_bitrate"
            return f"-b:a {rate}k"

        def __init__(self, **kwargs):
            made.append(kwargs)
            self.final_fn = Path("/tmp/output.opus")

        async def convert(self):
            pass

    Engine.lossless = lossless
    monkeypatch.setattr("streamrip.media.track.converter.get", lambda _: Engine)
    config = Config.defaults()
    config.session.conversion.lossy_bitrate = 160
    track = Track(
        None, None, config, Path(), None, None, download_path=Path("/tmp/source.flac")
    )

    await track._convert()

    assert made[0]["ffmpeg_arg"] == ffmpeg_arg
    assert track.download_path == Path("/tmp/output.opus")


def test_lossy_codec_quality_args_use_configured_bitrate():
    assert converter.OPUS.get_quality_arg(160) == "-b:a 160k"
    assert converter.AAC.get_quality_arg(192) == "-b:a 192k"
    assert converter.LAME.get_quality_arg(160) == "-b:a 160k"
    assert converter.Vorbis.get_quality_arg(160) == "-qscale:a 5"


@needs_ffmpeg
@pytest.mark.parametrize(
    ("codec", "extension"),
    [
        ("FLAC", "flac"),
        ("ALAC", "m4a"),
        ("AIFF", "aiff"),
        ("MP3", "mp3"),
        ("AAC", "m4a"),
        ("OPUS", "opus"),
        ("OGG", "ogg"),
    ],
)
async def test_conversion(tmp_path, codec, extension):
    source = tmp_path / "track.wav"  # any input ffmpeg can read
    shutil.copy(SILENCE, source)
    engine = converter.get(codec)(
        source, sampling_rate=48000, bit_depth=16, remove_source=True
    )

    await engine.convert()

    assert engine.final_fn == tmp_path / f"track.{extension}"
    assert mutagen.File(engine.final_fn) is not None
    assert not source.exists()


@needs_ffmpeg
async def test_a_16_bit_source_stays_16_bit_as_aiff(tmp_path):
    source = tmp_path / "track.flac"
    shutil.copy(SILENCE, source)
    engine = converter.AIFF(source, bit_depth=24)  # the default cap

    await engine.convert()

    assert mutagen.File(engine.final_fn).info.bits_per_sample == 16


@needs_ffmpeg
async def test_a_name_as_long_as_a_file_name_may_be_converts(tmp_path):
    """The temp file ffmpeg writes doesn't add to the name: it would not fit."""
    source = tmp_path / fit_filename("語" * 100, "flac")
    shutil.copy(SILENCE, source)
    engine = converter.LAME(source, remove_source=True)

    await engine.convert()

    assert mutagen.File(engine.final_fn) is not None


@needs_ffmpeg
async def test_failed_conversion_says_why_and_cleans_up(tmp_path):
    source = tmp_path / "track.flac"
    source.write_bytes(b"not audio")
    engine = converter.LAME(source, remove_source=True)

    with pytest.raises(ConversionError, match="ffmpeg failed") as err:
        await engine.convert()

    # ffmpeg's own words, not just that it failed (the wording varies).
    assert str(err.value).split("): ", 1)[1].strip()
    assert source.exists()
    assert not engine.tempfile.exists()


@needs_ffmpeg
async def test_a_result_that_cannot_be_put_in_place_keeps_the_source(
    tmp_path, monkeypatch
):
    source = tmp_path / "track.flac"
    shutil.copy(SILENCE, source)
    engine = converter.LAME(source, remove_source=True)

    def disk_full(*_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", disk_full)
    monkeypatch.setattr(Path, "move", disk_full)
    with pytest.raises(OSError, match="No space"):
        await engine.convert()

    assert source.exists()
