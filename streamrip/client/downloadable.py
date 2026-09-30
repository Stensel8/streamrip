import asyncio
import base64
import functools
import hashlib
import itertools
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiofiles
import aiohttp
import m3u8
import requests
from Cryptodome.Cipher import AES, Blowfish
from Cryptodome.Util import Counter

from .. import converter
from ..exceptions import FFmpegNotFoundError, NonStreamableError
from ..utils.ffmpeg_utils import find_ffmpeg

logger = logging.getLogger("streamrip")


BLOWFISH_SECRET = "g4el58wc0zvf9na1"
# Deezer encrypts the first 2048 bytes of every stripe of this many.
DEEZER_STRIPE = 3 * 2048


def generate_temp_path(url: str):
    return os.path.join(
        tempfile.gettempdir(),
        f"__streamrip_{hash(url)}_{time.time()}.download",
    )


def _plain_headers(headers) -> dict[str, str]:
    """Copy (aiohttp multidict) headers into a plain str dict for requests.

    requests rejects multidict's ``istr`` keys/values ("Header part ... must be
    of type str or bytes", upstream #941).
    """
    return {str(k): str(v) for k, v in dict(headers or {}).items()}


def _blocking_download(path, url, headers, report, stop: threading.Event, resume: bool):
    """Stream ``url`` into ``path`` with requests.

    Runs in a worker thread. With ``resume``, a partial file left by an
    earlier attempt that broke off (IncompleteRead, upstream #951/#1022) is
    continued with a Range request instead of starting over. Resuming is
    opt-in: appending to an unrelated existing file (a re-tagged track, a
    downscaled cover.jpg) would corrupt it.
    """
    chunk_size = 2**17  # 131 KB
    resume_pos = os.path.getsize(path) if resume and os.path.exists(path) else 0
    req_headers = dict(headers)
    if resume_pos > 0:
        req_headers["Range"] = f"bytes={resume_pos}-"

    with requests.get(
        url, headers=req_headers, allow_redirects=True, stream=True, timeout=60
    ) as resp:
        if resume_pos > 0 and resp.status_code == 416:
            # Range Not Satisfiable: the file is already complete.
            report(resume_pos)
            return
        resp.raise_for_status()
        # Only append if the server honoured the Range request (206). A 200
        # means it is sending the whole file again, so start over.
        if resume_pos > 0 and resp.status_code == 206:
            mode = "ab"
            report(resume_pos)
        else:
            mode = "wb"
        with open(path, mode) as file:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if stop.is_set():
                    raise asyncio.CancelledError
                file.write(chunk)
                report(len(chunk))


async def fast_async_download(path, url, headers, callback, resume: bool = False):
    """Download ``url`` to ``path`` without blocking the event loop.

    requests with large chunks is much faster than aiohttp's 1KB reads, but
    calling it on the event loop froze every other download while one
    connection was active (upstream PR #982). Run it in a worker thread and
    hand progress back to the loop, since rich's display is not thread-safe.
    """
    loop = asyncio.get_running_loop()
    stop = threading.Event()

    def report(n: int):
        try:
            loop.call_soon_threadsafe(callback, n)
        except RuntimeError:
            pass  # loop already closed (interpreter shutting down)

    try:
        await asyncio.to_thread(
            _blocking_download,
            path,
            url,
            _plain_headers(headers),
            report,
            stop,
            resume,
        )
    except asyncio.CancelledError:
        # The thread cannot be cancelled; tell it to stop at the next chunk.
        stop.set()
        raise


@dataclass(slots=True)
class Downloadable(ABC):
    session: aiohttp.ClientSession
    url: str
    extension: str
    source: str = "Unknown"
    _size_base: Optional[int] = None
    # Set by Track for retries: continue a partial file instead of restarting.
    resume: bool = False

    async def download(self, path: str, callback: Callable[[int], Any]):
        await self._download(path, callback)

    async def size(self) -> int:
        if hasattr(self, "_size") and self._size is not None:
            return self._size

        async with self.session.head(self.url) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length", 0)
            self._size = int(content_length)
            return self._size

    @property
    def _size(self):
        return self._size_base

    @_size.setter
    def _size(self, v):
        self._size_base = v

    @abstractmethod
    async def _download(self, path: str, callback: Callable[[int], None]):
        raise NotImplementedError


class BasicDownloadable(Downloadable):
    """Just downloads a URL."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        extension: str,
        source: str | None = None,
    ):
        self.session = session
        self.url = url
        self.extension = extension
        self._size = None
        self.source: str = source or "Unknown"
        self.resume = False

    async def _download(self, path: str, callback):
        await fast_async_download(
            path, self.url, self.session.headers, callback, resume=self.resume
        )


class DeezerDownloadable(Downloadable):
    is_encrypted = re.compile("/m(?:obile|edia)/")

    def __init__(self, session: aiohttp.ClientSession, info: dict):
        logger.debug("Deezer info for downloadable: %s", info)
        self.session = session
        self.url = info["url"]
        self.source: str = "deezer"
        qualities_available = [
            i for i, size in enumerate(info["quality_to_size"]) if size > 0
        ]
        if len(qualities_available) == 0:
            raise NonStreamableError(
                "Missing download info. Skipping.",
            )
        max_quality_available = max(qualities_available)
        self.quality = min(info["quality"], max_quality_available)
        self._size = info["quality_to_size"][self.quality]
        if self.quality <= 1:
            self.extension = "mp3"
        else:
            self.extension = "flac"
        self.id = str(info["id"])

    async def _download(self, path: str, callback):
        """Download the file, decrypting it on the fly if it's encrypted."""
        async with self.session.get(self.url, allow_redirects=True) as resp:
            resp.raise_for_status()
            self._size = int(resp.headers.get("Content-Length", 0))
            if self._size < 20000:
                # Too small to be audio: an error message in its place.
                try:
                    info = await resp.json(content_type=None)
                    message = f"{info['error']} - {info['message']}"
                except ValueError, KeyError, TypeError:
                    message = "File not found."
                raise NonStreamableError(message)

            if self.is_encrypted.search(self.url) is None:
                logger.debug(f"Deezer file at {self.url} not encrypted.")
                await fast_async_download(
                    path,
                    self.url,
                    self.session.headers,
                    callback,
                    resume=getattr(self, "resume", False),
                )
            else:
                blowfish_key = self._generate_blowfish_key(self.id)
                logger.debug(
                    "Deezer file (id %s) at %s is encrypted. Decrypting with %s",
                    self.id,
                    self.url,
                    blowfish_key,
                )

                # Decrypt as it arrives rather than holding the whole file in
                # memory: only an incomplete stripe is ever kept back.
                pending = bytearray()
                async with aiofiles.open(path, "wb") as audio:
                    async for data in resp.content.iter_chunked(2**17):
                        callback(len(data))
                        pending += data
                        whole = len(pending) - len(pending) % DEEZER_STRIPE
                        if whole:
                            chunk = pending[:whole]
                            del pending[:whole]
                            await audio.write(self._decrypt(blowfish_key, chunk))
                    await audio.write(self._decrypt(blowfish_key, pending))

    @classmethod
    def _decrypt(cls, key, data: bytearray) -> bytearray:
        """Decrypt stripes from the start of the stream (or right after the
        previous whole stripe): of every 3 * 2048 bytes, only the first 2048
        are encrypted.
        """
        for i in range(0, len(data), DEEZER_STRIPE):
            if len(data) - i >= 2048:
                data[i : i + 2048] = cls._decrypt_chunk(key, data[i : i + 2048])
        return data

    @staticmethod
    def _decrypt_chunk(key, data):
        """Decrypt a chunk of a Deezer stream.

        :param key:
        :param data:
        """
        return Blowfish.new(
            key,
            Blowfish.MODE_CBC,
            b"\x00\x01\x02\x03\x04\x05\x06\x07",
        ).decrypt(data)

    @staticmethod
    def _generate_blowfish_key(track_id: str) -> bytes:
        """Generate the blowfish key for Deezer downloads.

        :param track_id:
        :type track_id: str
        """
        md5_hash = hashlib.md5(track_id.encode()).hexdigest()
        # good luck :)
        return "".join(
            chr(functools.reduce(lambda x, y: x ^ y, map(ord, t)))
            for t in zip(md5_hash[:16], md5_hash[16:], BLOWFISH_SECRET)
        ).encode()


class TidalDownloadable(Downloadable):
    """A wrapper around BasicDownloadable that includes Tidal-specific
    error messages.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str | None,
        codec: str,
        encryption_key: str | None,
        restrictions,
    ):
        self.session = session
        self.source = "tidal"
        codec = codec.lower()
        if codec in ("flac", "mqa"):
            self.extension = "flac"
        else:
            self.extension = "m4a"

        if url is None:
            # Turn CamelCase code into a readable sentence
            if restrictions:
                words = re.findall(r"([A-Z][a-z]+)", restrictions[0]["code"])
                raise NonStreamableError(
                    words[0] + " " + " ".join(map(str.lower, words[1:])),
                )
            raise NonStreamableError(
                f"Tidal download: dl_info = {url, codec, encryption_key}"
            )
        self.url = url
        self.enc_key = encryption_key
        self.downloadable = BasicDownloadable(session, url, self.extension, "tidal")

    async def _download(self, path: str, callback):
        self.downloadable.resume = getattr(self, "resume", False)
        await self.downloadable._download(path, callback)
        if self.enc_key is not None:
            dec_bytes = await self._decrypt_mqa_file(path, self.enc_key)
            async with aiofiles.open(path, "wb") as audio:
                await audio.write(dec_bytes)

    @property
    def _size(self):
        return self.downloadable._size

    @_size.setter
    def _size(self, v):
        self.downloadable._size = v

    @staticmethod
    async def _decrypt_mqa_file(in_path, encryption_key):
        """Decrypt an MQA file.

        :param in_path:
        :param out_path:
        :param encryption_key:
        """

        # Do not change this
        master_key = "UIlTTEMmmLfGowo/UC60x2H45W6MdGgTRfo/umg4754="

        # Decode the base64 strings to ascii strings
        master_key = base64.b64decode(master_key)
        security_token = base64.b64decode(encryption_key)

        # Get the IV from the first 16 bytes of the securityToken
        iv = security_token[:16]
        encrypted_st = security_token[16:]

        # Initialize decryptor
        decryptor = AES.new(master_key, AES.MODE_CBC, iv)

        # Decrypt the security token
        decrypted_st = decryptor.decrypt(encrypted_st)

        # Get the audio stream decryption key and nonce from the decrypted security token
        key = decrypted_st[:16]
        nonce = decrypted_st[16:24]

        counter = Counter.new(64, prefix=nonce, initial_value=0)
        decryptor = AES.new(key, AES.MODE_CTR, counter=counter)

        async with aiofiles.open(in_path, "rb") as enc_file:
            dec_bytes = decryptor.decrypt(await enc_file.read())
            return dec_bytes


class TidalDASHDownloadable(Downloadable):
    """Tidal hi-res tracks served as an MPEG-DASH manifest (upstream PR #998).

    The init segment and every media segment are fetched into a fragmented
    MP4, which ffmpeg then remuxes (stream copy, bit-identical audio) into the
    final container.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        init_url: str,
        segment_urls: list[str],
        codec: str,
    ):
        self.session = session
        self.source = "tidal"
        self.url = init_url
        self.init_url = init_url
        self.segment_urls = segment_urls
        self.extension = "flac" if codec.lower() in ("flac", "mqa") else "m4a"
        self._size = None

    async def size(self) -> int:
        """Total size of the init segment plus every media segment.

        HEAD-ing only self.url would report the size of the tiny init segment,
        so the progress bar would jump past 100% immediately. Requests are
        bounded: firing one per segment at once gets many of them refused.
        """
        if self._size is not None:
            return self._size

        sem = asyncio.Semaphore(8)

        async def content_length(url: str) -> int | None:
            async with sem:
                try:
                    async with self.session.head(url) as resp:
                        resp.raise_for_status()
                        return int(resp.headers.get("Content-Length", 0))
                except Exception:
                    return None

        sizes = await asyncio.gather(
            *(content_length(u) for u in (self.init_url, *self.segment_urls))
        )
        known = [s for s in sizes if s]
        if not known:
            self._size = 0
            return 0
        # Segments are near-uniform; estimate the ones that did not answer.
        average = sum(known) // len(known)
        self._size = sum(known) + (len(sizes) - len(known)) * average
        return self._size

    async def _download(self, path: str, callback):
        """Fetch DASH segments and remux them to path, reporting bytes received.

        Raise FFmpegNotFoundError before fetching if ffmpeg is unavailable.
        Remove the temporary MP4 even if downloading or remuxing fails.
        """
        ffmpeg_path = find_ffmpeg()
        if ffmpeg_path is None:
            raise FFmpegNotFoundError(
                "ffmpeg not found, which Tidal hi-res (DASH) tracks need"
            )
        tmp_path = path + ".dash.mp4"
        try:
            async with aiofiles.open(tmp_path, "wb") as f:
                for url in (self.init_url, *self.segment_urls):
                    async with self.session.get(url) as resp:
                        resp.raise_for_status()
                        chunk = await resp.read()
                    await f.write(chunk)
                    callback(len(chunk))

            # -map_metadata -1 -fflags +bitexact: a plain stream-copy remux
            # would otherwise leave the source MP4's own container fields
            # and ffmpeg's version stamp sitting in the output's tags,
            # meaningless outside that MP4. streamrip's own tagger writes
            # the real tags right after this step anyway.
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_path,
                "-i",
                tmp_path,
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-c",
                "copy",
                "-y",
                path,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not os.path.isfile(path):
                # Without this a failed remux is silent and the track would be
                # recorded as downloaded with nothing on disk.
                raise NonStreamableError(
                    f"ffmpeg failed to remux Tidal DASH stream (exit "
                    f"{proc.returncode}): {stderr.decode(errors='replace')[-300:]}"
                )
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


class SoundcloudDownloadable(Downloadable):
    def __init__(self, session, info: dict):
        self.session = session
        self.file_type = info["type"]
        self.source = "soundcloud"
        if self.file_type in ("mp3", "progressive"):
            self.extension = "mp3"
        elif self.file_type == "original":
            self.extension = "flac"
        else:
            raise Exception(f"Invalid file type: {self.file_type}")
        self.url = info["url"]

    async def _download(self, path, callback):
        if self.file_type == "mp3":
            await self._download_mp3(path, callback)
        elif self.file_type == "progressive":
            await BasicDownloadable(
                self.session, self.url, "mp3", source="soundcloud"
            ).download(path, callback)
        else:
            await self._download_original(path, callback)

    async def _download_original(self, path: str, callback):
        """Download the original file and convert it to FLAC."""
        downloader = BasicDownloadable(
            self.session, self.url, "flac", source="soundcloud"
        )
        await downloader.download(path, callback)
        await converter.FLAC(path).convert()

    async def _download_mp3(self, path: str, callback):
        """Download every HLS segment concurrently, then concatenate them."""
        # TODO: make progress bar reflect bytes
        async with self.session.get(self.url) as resp:
            content = await resp.text("utf-8")

        parsed_m3u = m3u8.loads(content)
        self._size = len(parsed_m3u.segments)
        # Segments finish in any order; each has its own path, so they're
        # concatenated in playlist order, and all removed afterwards -- also
        # when one fails, or they pile up in the temp dir.
        segment_paths = [generate_temp_path(s.uri) for s in parsed_m3u.segments]
        tasks = [
            asyncio.create_task(self._download_segment(segment.uri, tmp, callback))
            for segment, tmp in zip(parsed_m3u.segments, segment_paths)
        ]
        try:
            await asyncio.gather(*tasks)
            await concat_audio_files(segment_paths, path, "mp3")
        finally:
            for task in tasks:
                task.cancel()
            # Let cancelled downloads finish unwinding before deleting their
            # files, or one could still create its file afterwards.
            await asyncio.gather(*tasks, return_exceptions=True)
            for tmp in segment_paths:
                if os.path.exists(tmp):
                    os.remove(tmp)

    async def _download_segment(self, segment_uri: str, tmp: str, callback):
        """Download one HLS segment to tmp and report it done."""
        async with self.session.get(segment_uri) as resp:
            resp.raise_for_status()
            async with aiofiles.open(tmp, "wb") as file:
                await file.write(await resp.content.read())
        callback(1)

    async def size(self) -> int:
        if self.file_type == "mp3":
            async with self.session.get(self.url) as resp:
                content = await resp.text("utf-8")

            parsed_m3u = m3u8.loads(content)
            self._size = len(parsed_m3u.segments)
        return await super().size()


async def concat_audio_files(paths: list[str], out: str, ext: str, max_files_open=128):
    """Concatenate audio files using FFmpeg. Batched by max files open.

    Recurses log_{max_file_open}(len(paths)) times.
    """
    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path is None:
        raise FFmpegNotFoundError(
            "FFmpeg is required. Install it (apt/brew/winget install ffmpeg), or "
            "run: pip install imageio-ffmpeg"
        )

    # Base case
    if len(paths) == 1:
        shutil.move(paths[0], out)
        return

    it = iter(paths)
    num_batches = len(paths) // max_files_open + (
        1 if len(paths) % max_files_open != 0 else 0
    )
    tempdir = tempfile.gettempdir()
    outpaths = [
        os.path.join(
            tempdir,
            f"__streamrip_ffmpeg_{hash(paths[i * max_files_open])}.{ext}",
        )
        for i in range(num_batches)
    ]

    for p in outpaths:
        try:
            os.remove(p)  # in case of failure
        except FileNotFoundError:
            pass

    proc_futures = []
    for i in range(num_batches):
        command = (
            ffmpeg_path,
            "-i",
            f"concat:{'|'.join(itertools.islice(it, max_files_open))}",
            "-acodec",
            "copy",
            "-loglevel",
            "warning",
            outpaths[i],
        )
        fut = asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        proc_futures.append(fut)

    # Create all processes concurrently
    processes = await asyncio.gather(*proc_futures)

    # wait for all of them to finish
    results = await asyncio.gather(*[p.communicate() for p in processes])
    try:
        for proc, (_, stderr) in zip(processes, results):
            if proc.returncode != 0:
                raise Exception(
                    f"FFMPEG returned with status code {proc.returncode}: "
                    f"{stderr.decode(errors='replace')[-300:]}"
                )

        # Recurse on remaining batches
        await concat_audio_files(outpaths, out, ext)
    finally:
        # The last one was moved into place; the rest are intermediates.
        for p in outpaths:
            if os.path.exists(p):
                os.remove(p)
