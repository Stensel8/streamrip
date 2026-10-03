"""Tests for resumable downloads in fast_async_download.

These spin up a local HTTP server that interrupts the first response
mid-stream (reproducing the IncompleteRead errors seen on large tracks)
and verify that a subsequent attempt resumes and produces a byte-for-byte
correct file.
"""

import hashlib
import http.server
import os
import threading

import pytest

from streamrip.client.downloadable import _content_range, fast_async_download
from streamrip.exceptions import IncompleteDownloadError


def _payload(n: int) -> bytes:
    return bytes((i * 31 + 7) % 256 for i in range(n))


def _start_server(handler_cls):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


async def test_resume_with_range_support(tmp_path):
    """Server drops the connection mid-stream, then honors Range with 206."""
    total = 1_000_000
    payload = _payload(total)
    full_sha = hashlib.sha256(payload).hexdigest()
    request_ranges = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            request_ranges.append(rng)
            if rng is None:
                self.send_response(200)
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(payload[: total // 2])
                self.wfile.flush()
                self.close_connection = True
                self.connection.close()
            else:
                start = int(rng.replace("bytes=", "").split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
                self.send_header("Content-Length", str(total - start))
                self.end_headers()
                self.wfile.write(payload[start:])

    srv, port = _start_server(Handler)
    url = f"http://127.0.0.1:{port}/track.flac"
    path = str(tmp_path / "track.flac")

    last_exc = None
    for attempt in range(4):
        try:
            await fast_async_download(path, url, {}, lambda n: None, resume=attempt > 0)
            last_exc = None
            break
        except Exception as e:
            last_exc = e

    srv.shutdown()

    assert last_exc is None
    assert os.path.getsize(path) == total
    with open(path, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == full_sha
    # The retry sent a Range header (resume actually happened).
    assert any(r and r.startswith("bytes=") for r in request_ranges)


async def test_resume_falls_back_when_range_ignored(tmp_path):
    """If the server ignores Range and replies 200 with the full body,
    the partial file must be overwritten, not appended to."""
    total = 600_000
    payload = _payload(total)
    full_sha = hashlib.sha256(payload).hexdigest()
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            calls.append(self.headers.get("Range"))
            if len(calls) == 1:
                self.send_response(200)
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(payload[: total // 2])
                self.wfile.flush()
                self.close_connection = True
                self.connection.close()
            else:
                # Range ignored: send the whole file from the start.
                self.send_response(200)
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(payload)

    srv, port = _start_server(Handler)
    url = f"http://127.0.0.1:{port}/x.flac"
    path = str(tmp_path / "x.flac")

    for attempt in range(4):
        try:
            await fast_async_download(path, url, {}, lambda n: None, resume=attempt > 0)
            break
        except Exception:
            pass

    srv.shutdown()

    # No duplicated bytes despite the partial file already existing.
    assert os.path.getsize(path) == total
    with open(path, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == full_sha


async def test_existing_file_is_overwritten_without_resume(tmp_path):
    """A first attempt must never append to an unrelated existing file.

    Covers re-downloading over a tagged track or a downscaled cover.jpg, which
    would otherwise be "resumed" into a corrupt file.
    """
    payload = _payload(50_000)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            assert self.headers.get("Range") is None
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv, port = _start_server(Handler)
    path = tmp_path / "cover.jpg"
    path.write_bytes(b"old, smaller file")
    await fast_async_download(
        str(path), f"http://127.0.0.1:{port}/c.jpg", {}, lambda n: None
    )
    srv.shutdown()
    assert path.read_bytes() == payload


async def test_multidict_headers_are_accepted(tmp_path):
    """aiohttp's istr header keys used to make requests refuse the call (#941)."""
    from multidict import CIMultiDict

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["ua"] = self.headers.get("User-Agent")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    srv, port = _start_server(Handler)
    headers = CIMultiDict({"User-Agent": "streamrip-test"})
    await fast_async_download(
        str(tmp_path / "f"), f"http://127.0.0.1:{port}/", headers, lambda n: None
    )
    srv.shutdown()
    assert seen["ua"] == "streamrip-test"


def _interrupting_handler(payload, on_range):
    """Drop the first response halfway; answer Range requests with on_range."""
    total = len(payload)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            if rng is None:
                self.send_response(200)
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(payload[: total // 2])
                self.wfile.flush()
                self.close_connection = True
                self.connection.close()
            else:
                start = int(rng.replace("bytes=", "").split("-")[0])
                on_range(self, start)

    return Handler


async def test_resume_at_the_wrong_offset_starts_over(tmp_path):
    """A 206 that does not start where the partial file ends is not appended.

    Appending it would leave a corrupt file of plausible size; it used to go
    through, since urllib3 only checks the 206 body against its own length.
    """
    total = 1_000_000
    payload = _payload(total)

    def wrong_offset(handler, start):
        off = start - 1000  # the server answers from the wrong byte
        handler.send_response(206)
        handler.send_header("Content-Range", f"bytes {off}-{total - 1}/{total}")
        handler.send_header("Content-Length", str(total - off))
        handler.end_headers()
        handler.wfile.write(payload[off:])

    srv, port = _start_server(_interrupting_handler(payload, wrong_offset))
    url = f"http://127.0.0.1:{port}/track.flac"
    path = str(tmp_path / "track.flac")

    with pytest.raises(Exception):  # the dropped first response
        await fast_async_download(path, url, {}, lambda n: None)
    assert os.path.exists(path)
    with pytest.raises(IncompleteDownloadError, match="resume at byte"):
        await fast_async_download(path, url, {}, lambda n: None, resume=True)
    srv.shutdown()

    # Removed, so the next attempt starts from scratch instead of resuming
    # onto the same mismatch.
    assert not os.path.exists(path)


async def test_resume_that_does_not_add_up_to_the_total_starts_over(tmp_path):
    total = 1_000_000
    payload = _payload(total)

    def wrong_total(handler, start):
        handler.send_response(206)
        # Right offset, but the announced total disagrees with what is sent.
        handler.send_header("Content-Range", f"bytes {start}-{total - 1}/{total + 5}")
        handler.send_header("Content-Length", str(total - start))
        handler.end_headers()
        handler.wfile.write(payload[start:])

    srv, port = _start_server(_interrupting_handler(payload, wrong_total))
    url = f"http://127.0.0.1:{port}/track.flac"
    path = str(tmp_path / "track.flac")

    with pytest.raises(Exception):
        await fast_async_download(path, url, {}, lambda n: None)
    with pytest.raises(IncompleteDownloadError, match="announced"):
        await fast_async_download(path, url, {}, lambda n: None, resume=True)
    srv.shutdown()

    assert not os.path.exists(path)


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


async def test_resume_with_unknown_total_that_stops_early_starts_over(tmp_path):
    """`bytes <start>-<end>/*` leaves the total unknown, and a 206 without
    Content-Length is delimited by the connection closing, so urllib3 cannot
    tell it ended early. The range end must still be reached."""
    total = 1_000_000
    payload = _payload(total)

    def unknown_total_cut_short(handler, start):
        handler.send_response(206)
        handler.send_header("Content-Range", f"bytes {start}-{total - 1}/*")
        handler.end_headers()  # no Content-Length: close-delimited
        handler.wfile.write(payload[start : start + 1000])
        handler.wfile.flush()
        handler.close_connection = True

    srv, port = _start_server(_interrupting_handler(payload, unknown_total_cut_short))
    url = f"http://127.0.0.1:{port}/track.flac"
    path = str(tmp_path / "track.flac")

    with pytest.raises(Exception):
        await fast_async_download(path, url, {}, lambda n: None)
    with pytest.raises(IncompleteDownloadError, match="announced"):
        await fast_async_download(path, url, {}, lambda n: None, resume=True)
    srv.shutdown()

    assert not os.path.exists(path)


def _answer_416(total):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


async def test_416_accepts_a_partial_file_of_the_announced_length(tmp_path):
    srv, port = _start_server(_answer_416(1000))
    path = tmp_path / "track.flac"
    path.write_bytes(b"x" * 1000)

    await fast_async_download(
        str(path), f"http://127.0.0.1:{port}/t", {}, lambda n: None, resume=True
    )
    srv.shutdown()

    assert path.read_bytes() == b"x" * 1000


async def test_416_for_a_partial_file_of_another_length_starts_over(tmp_path):
    """A 416 used to mean "complete" whatever the local size; a 1,200-byte file
    against "bytes */1000" was kept although it is not the server's file."""
    srv, port = _start_server(_answer_416(1000))
    path = tmp_path / "track.flac"
    path.write_bytes(b"x" * 1200)

    with pytest.raises(IncompleteDownloadError, match="416"):
        await fast_async_download(
            str(path), f"http://127.0.0.1:{port}/t", {}, lambda n: None, resume=True
        )
    srv.shutdown()

    assert not path.exists()
