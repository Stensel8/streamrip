"""Downloads that resume, the ffmpeg they need, and the conversions it does."""

import hashlib
import http.server
import shutil
import threading
import time
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
    SegmentedMP4Downloadable,
    SoundcloudDownloadable,
    fast_async_download,
)
from streamrip.config import set_user_defaults
from streamrip.exceptions import (
    ConversionError,
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


def _drop_after(handler, body):
    """Send `body` of a TOTAL bytes file, then hang up (an IncompleteRead)."""
    _reply(handler, 200, body, {"Content-Length": str(TOTAL)})
    handler.wfile.flush()
    handler.close_connection = True
    handler.connection.close()


def _partial(handler, start, total=TOTAL):
    headers = {
        "Content-Range": f"bytes {start}-{TOTAL - 1}/{total}",
        "Content-Length": str(TOTAL - start),
    }
    _reply(handler, 206, PAYLOAD[start:], headers)


def _interrupted(on_range):
    """Drop the first response halfway; answer a Range request with
    on_range(handler, start)."""

    def answer(handler):
        if (rng := handler.headers.get("Range")) is None:
            _drop_after(handler, PAYLOAD[: TOTAL // 2])
        else:
            on_range(handler, int(rng.removeprefix("bytes=").split("-")[0]))

    return answer


@pytest.mark.parametrize(
    ("on_range", "error"),
    [
        (_partial, None),
        # Range ignored: overwritten, not appended to.
        (lambda h, _: _reply(h, 200, PAYLOAD, {"Content-Length": str(TOTAL)}), None),
        # Appended, this would leave a corrupt file of plausible size.
        (lambda h, start: _partial(h, start, total=TOTAL + 5), "announced"),
    ],
    ids=["206", "200", "wrong total"],
)
async def test_an_interrupted_download_resumes_or_starts_over(
    server, tmp_path, on_range, error
):
    url = server(_interrupted(on_range))
    path = tmp_path / "track.flac"
    with pytest.raises(requests.RequestException):  # the dropped first response
        await fast_async_download(path, url, {}, _ignore)

    if error is None:
        await fast_async_download(path, url, {}, _ignore, resume=True)
        assert hashlib.sha256(path.read_bytes()).digest() == PAYLOAD_SHA
    else:
        with pytest.raises(IncompleteDownloadError, match=error):
            await fast_async_download(path, url, {}, _ignore, resume=True)
        # Removed, so the next attempt starts over instead of resuming onto
        # the same mismatch.
        assert not path.exists()


async def test_what_arrived_before_a_break_is_kept_and_resumed(server, tmp_path):
    # #122: Qobuz's CDN hung up after the first byte of a track, every time.
    # That byte was lost with the chunk it was read into, so the part file
    # stayed empty and every retry was the same request from the start.
    def answer(handler):
        if handler.headers.get("Range") is None:
            _drop_after(handler, PAYLOAD[:1])
        else:
            _partial(handler, 1)

    url = server(answer)
    path = tmp_path / "track.part.flac"
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        await fast_async_download(path, url, {}, _ignore)
    assert path.read_bytes() == PAYLOAD[:1]

    await fast_async_download(path, url, {}, _ignore, resume=True)
    assert hashlib.sha256(path.read_bytes()).digest() == PAYLOAD_SHA


@pytest.mark.parametrize(("size", "kept"), [(1000, True), (1200, False)])
async def test_a_416_keeps_only_a_partial_file_of_the_announced_length(
    server, tmp_path, size, kept
):
    # A 416 used to mean "complete" whatever the local size.
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
    # A re-download over a tagged track or a downscaled cover.jpg would
    # otherwise be "resumed" into a corrupt file.
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
            await file.download(tmp_path / "file.flac", _ignore)
    assert asked["verify"] is verify_ssl


async def test_multidict_headers_are_accepted(server, tmp_path):
    # aiohttp's istr header keys used to make requests refuse the call (#941).
    seen = {}

    def answer(handler):
        seen["ua"] = handler.headers.get("User-Agent")
        _reply(handler, 200, b"ok", {"Content-Length": "2"})

    headers = CIMultiDict({"User-Agent": "streamrip-test"})
    await fast_async_download(tmp_path / "f", server(answer), headers, _ignore)
    assert seen["ua"] == "streamrip-test"


@pytest.mark.login
def test_a_missing_ffmpeg_is_a_message_before_any_login(tmp_path):
    cfg = tmp_path / "config.toml"
    set_user_defaults(cfg)
    with (
        patch("streamrip.utils.ffmpeg_utils.find_ffmpeg", return_value=None),
        patch.object(Main, "get_logged_in_client", AsyncMock()) as login,
        # The console may hold on to the stdout it was created with, so check
        # what is printed instead of what CliRunner captures.
        patch("streamrip.rip.cli.console") as console,
    ):
        result = CliRunner().invoke(
            rip, ["--config-path", str(cfg), "url", "https://tidal.com/album/1"]
        )
    printed = "".join(str(call.args[0]) for call in console.print.call_args_list)
    assert result.exit_code == 1
    assert "No ffmpeg installation found" in printed
    login.assert_not_called()


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
    tmp_path, served, tried
):
    # #122
    track, asked = _failing_track(tmp_path, served)
    with (
        patch("streamrip.media.track.format_quality", return_value="FLAC"),
        patch("streamrip.media.track.asyncio.sleep", AsyncMock()),
    ):
        await track.download()
    assert track.download_path.read_bytes() == b"fLaC"
    expected = [t for t in tried[:-1] for _ in range(MAX_DOWNLOAD_ATTEMPTS)]
    assert asked == [*expected, 2]


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


@needs_ffmpeg
@pytest.mark.parametrize(
    ("codec", "extension"),
    [("FLAC", "flac"), ("ALAC", "m4a"), ("MP3", "mp3"), ("OPUS", "opus")],
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


@pytest.mark.parametrize(
    ("source", "rates"),
    [
        (88200, "44100"),
        (176400, "44100"),
        (96000, "48000"),
        (32000, "32000"),  # under the cap: never raised
        (None, "44100|48000"),
    ],
)
def test_a_capped_rate_keeps_to_the_family_of_the_source(monkeypatch, source, rates):
    # ffmpeg takes the allowed rate closest to the source's: 48 kHz for 88.2.
    monkeypatch.setattr(converter, "_sample_rate", lambda path: source)

    args = converter.FLAC(Path("track.flac"), sampling_rate=48000)._ffmpeg_args()

    assert f"aformat=sample_rates={rates}" in args


async def test_segments_are_sized_with_two_requests_not_one_each():
    session = MagicMock()
    answer = MagicMock(headers={"Content-Length": "100"})
    session.head.return_value.__aenter__.return_value = answer
    segments = [f"https://cdn.test/{n}.mp4" for n in range(100)]
    dl = SegmentedMP4Downloadable(
        session, "https://cdn.test/init.mp4", segments, "flac", "tidal"
    )

    assert await dl.size() == 100 + 100 * 100
    assert session.head.call_count == 2


@needs_ffmpeg
async def test_an_hls_playlist_is_fetched_once_for_its_size_and_its_audio(tmp_path):
    session = MagicMock()
    answer = MagicMock(text=AsyncMock(return_value="#EXTM3U\n#EXTINF:10,\nseg.mp3\n"))
    session.get.return_value.__aenter__.return_value = answer
    answer.content.read = AsyncMock(return_value=b"mp3")
    playlist = "https://sc.test/playlist.m3u8"
    dl = SoundcloudDownloadable(session, playlist, "hls")

    await dl.size()
    await dl.download(tmp_path / "track.mp3", _ignore)

    assert [c.args[0] for c in session.get.call_args_list].count(playlist) == 1
    assert (tmp_path / "track.mp3").read_bytes() == b"mp3"


@needs_ffmpeg
async def test_a_name_as_long_as_a_file_name_may_be_converts(tmp_path):
    # The temp file ffmpeg writes doesn't add to the name: it would not fit.
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

    with pytest.raises(ConversionError, match="ffmpeg failed"):
        await engine.convert()

    assert source.exists()
    assert not engine.tempfile.exists()
