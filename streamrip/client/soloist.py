"""Spotify's own audio, played by Spotify's official headless client.

Spotify Soloist (https://github.com/spotify/soloist) is a Spotify Connect
device that plays a track and renders it to local audio at the account's
quality (lossless for Premium). The `spotify` backend plays a track in its
single-track mode and records the local audio -- a PipeWire or PulseAudio
sink's monitor, which is exactly what the account gets, bit-exact -- and
encodes it as FLAC.

Three things it manages, all one-time or automatic:

  - the binary: the official builds expire after 90 days (they exit with
    code 10); this module downloads them from Spotify's CDN to its own
    location and refreshes them when an expired one shows up;
  - the key: one Soloist API key per person, in the [spotify] config's
    `soloist_api_key`; it is a secret and is kept out of everything that is
    printed or raised;
  - the pairing: once, a Spotify app on the same network picks the device
    (the config's `device_name`) and plays; the session is stored in
    Soloist's data dir and replayed by every later run.

Linux only for now: the recording is a sink monitor, which is a
PipeWire/PulseAudio thing. On other platforms the audio comes from the
youtube backend instead (a NonStreamableError, which the track's fallback
turns into a YouTube Music download).
"""

import contextlib
import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path
lazy from collections.abc import Callable

lazy import requests

from ..exceptions import AuthenticationError, NonStreamableError
from ..utils.ffmpeg_utils import require_ffmpeg
lazy from ..config import SpotifyConfig

logger = logging.getLogger("streamrip")

# The official builds: one archive per platform, each holding the one
# executable named soloist (or soloist.exe).
BUILDS = "https://soloist-builds.spotifycdn.com/"
# A run that has not said "playing" this long after it started is a run that
# will not (no session, or no play permission): fail it, and let the track
# go to the youtube backend.
PLAYING_WAIT = 90.0
# Past the track's own length: startup and the sink's last frames on top.
PLAY_MARGIN = 60.0
# The sink Soloist renders into and streamrip records, one per machine.
SINK = "soloist_cap"
# Soloist's exit code for a build past its 90 days.
EXPIRED_BUILD = 10
UNSUPPORTED_PLATFORM = (
    "Spotify's own audio is not supported on this platform yet; "
    "the audio comes from YouTube Music"
)

# Soloist timestamps every line ("2026-11-17 00:17:43.429: playing ..."),
# so neither marker is anchored to the line's start.
_POSITION = re.compile(r"\[(\d+):(\d+) / \d+:\d+\]$")
_LOGGED_IN = re.compile(r"logged in as (\S+)")


class _ExpiredBuildError(Exception):
    """The build has outlived its 90 days: a fresh one, and one more try."""


def build_archive() -> str | None:
    """The name of the official build archive for this machine, or None."""
    machine = platform.machine()
    if sys.platform == "win32" and machine in {"AMD64", "x86_64"}:
        return "soloist_release_windows_x86_64.zip"
    if sys.platform == "darwin" and machine == "arm64":
        return "soloist_release_osx_arm64.tar.gz"
    if sys.platform.startswith("linux"):
        return {
            "x86_64": "soloist_release_x86_64.tar.gz",
            "amd64": "soloist_release_x86_64.tar.gz",
            "aarch64": "soloist_release_arm64.tar.gz",
            "armv7l": "soloist_release_arm32.tar.gz",
        }.get(machine)
    return None


def is_playing_line(line: str, uri: str) -> bool:
    """Whether `line` says Soloist began playing `uri`.

    Matched mid-line, since Soloist timestamps its lines; the id is matched
    whole ("...<id> "), since a track whose id is a prefix of another's
    would otherwise match both.
    """
    return f"playing {uri} " in line


def line_position_s(line: str) -> float | None:
    """The seconds into the track a "playing ... [m:ss / m:ss]" line says,
    or None for any other line."""
    if m := _POSITION.search(line):
        return int(m[1]) * 60 + int(m[2])
    return None


def summarize_log(lines: list[str], uri: str) -> tuple[bool, float, str | None]:
    """What a Soloist run told us: (it began playing `uri`, the furthest
    position in seconds it reached, the username it logged in as)."""
    started, furthest, username = False, 0.0, None
    for line in lines:
        if not started and is_playing_line(line, uri):
            started = True
        if pos := line_position_s(line):
            furthest = max(furthest, pos)
        if m := _LOGGED_IN.search(line):
            username = m[1]
    return started, furthest, username


def tail(lines: list[str], how_many: int = 5) -> str:
    """The end of what a process said, for an error message."""
    said = " ".join(line.strip() for line in lines[-how_many:] if line.strip())
    return said[:300] or "(it said nothing)"


def run_cmd(cmd: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    """One short command, captured: pactl, ffprobe, ffmpeg's volumedetect,
    soloist --version."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def start_cmd(cmd: list[str], quiet: bool = False) -> subprocess.Popen:
    """One long process: Soloist (its lines piped) or the recorder (quiet)."""
    if quiet:
        return subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _read_until_done(proc: subprocess.Popen, timeout_s: float) -> list[str]:
    """Every line the process prints until it exits, within `timeout_s`.

    The lines are read on a thread: a process that goes quiet -- or exits
    quietly -- must not hold the deadline hostage.
    """
    pending = queue.Queue()

    def read():
        for line in proc.stdout:
            pending.put(line)

    threading.Thread(target=read, daemon=True).start()
    lines: list[str] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(queue.Empty):
            lines.append(pending.get(timeout=1.0))
        if proc.poll() is not None and pending.empty():
            break
    else:
        proc.kill()
    while not pending.empty():
        lines.append(pending.get_nowait())
    return [line.rstrip("\n") for line in lines]


class SoloistBackend:
    """Spotify's own audio for this process: the binary, the sink, and the
    play-and-record of a track.

    Everything here blocks; callers run it in a worker thread.
    """

    def __init__(self, config: SpotifyConfig):
        self.device_name = config.device_name or "streamrip"
        self.api_key = config.soloist_api_key or ""
        # Track ids Spotify already refused to play: this process does not
        # ask again, and the client routes those tracks to YouTube Music.
        self.refused: set[str] = set()
        # One machine at a time may download a build.
        self._binary_lock = threading.Lock()

    @property
    def supported(self) -> bool:
        """Linux: the recording is a sink monitor, a PipeWire/PulseAudio thing."""
        return sys.platform.startswith("linux")

    # --- the binary ----------------------------------------------------------

    def _app_binary(self) -> Path:
        """Where streamrip keeps its own soloist (not one the user installed)."""
        if sys.platform == "win32":
            base = Path(
                os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
            )
            return base / "streamrip" / "soloist" / "soloist.exe"
        return Path.home() / ".local" / "share" / "streamrip" / "soloist" / "soloist"

    @staticmethod
    def _runs(binary: Path) -> bool:
        """Whether the binary even starts (a 90-days-expired one refuses)."""
        try:
            return run_cmd([str(binary), "--version"]).returncode == 0
        except OSError, subprocess.SubprocessError:
            return False

    def ensure_binary(self) -> Path:
        """A working soloist: one on PATH, or the one streamrip keeps itself."""
        if (found := shutil.which("soloist")) is not None:
            return Path(found)
        path = self._app_binary()
        if not path.is_file() or not self._runs(path):
            with self._binary_lock:
                if not path.is_file() or not self._runs(path):
                    self._download_binary(path)
        return path

    def refresh_binary(self) -> Path:
        """A fresh build, over the one that has expired, and its path."""
        path = self._app_binary()
        with self._binary_lock:
            self._download_binary(path)
        return path

    def _download_binary(self, path: Path):
        """Download and install this platform's build, at path, in one step."""
        name = build_archive()
        if name is None:
            raise NonStreamableError(UNSUPPORTED_PLATFORM)
        url = BUILDS + name
        path.parent.mkdir(parents=True, exist_ok=True)
        # A scratch dir beside the final home: the swap is one rename, and a
        # build that dies half-installed leaves the old one in place.
        scratch = Path(tempfile.mkdtemp(prefix="__streamrip_soloist_", dir=path.parent))
        try:
            archive = scratch / name
            with requests.get(url, stream=True, timeout=30) as resp:
                if resp.status_code != 200:
                    raise NonStreamableError(
                        f"Could not download Soloist: HTTP {resp.status_code} "
                        f"from {BUILDS}"
                    )
                with archive.open("wb") as f:
                    for chunk in resp.iter_content(2**16):
                        f.write(chunk)
            if name.endswith(".zip"):
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(scratch)
            else:
                with tarfile.open(archive) as tar:
                    tar.extractall(scratch, filter="data")
            found = [
                f for f in scratch.rglob("soloist*") if f.is_file() and f != archive
            ]
            if not found:
                raise NonStreamableError("The Soloist build has no executable in it")
            binary = found[0]
            if os.name == "posix":
                binary.chmod(0o755)
            binary.replace(path)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if not self._runs(path):
            raise NonStreamableError("The downloaded Soloist build will not run")

    # --- the sink --------------------------------------------------------------

    def ensure_sink(self) -> None:
        """The virtual sink Soloist renders into, created on first use."""
        if shutil.which("pactl") is None:
            raise NonStreamableError(UNSUPPORTED_PLATFORM)
        listed = run_cmd(["pactl", "list", "short", "sinks"])
        if listed.returncode != 0:
            raise NonStreamableError(UNSUPPORTED_PLATFORM)
        for line in listed.stdout.splitlines():
            # pactl's short form: <index> <name> <description> ...
            parts = line.split()
            if len(parts) >= 2 and parts[1] == SINK:
                return
        loaded = run_cmd(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={SINK}",
                "sink_properties=device.description=streamrip capture",
            ]
        )
        if loaded.returncode != 0:
            said = loaded.stderr.strip()[:300]
            raise NonStreamableError(f"Could not create the capture sink: {said}")

    # --- the pairing -------------------------------------------------------------

    def paired(self) -> bool:
        """Whether a pairing was already stored: the session lives under
        <data dir>/cache/Users. A heuristic only -- playback is the check."""
        data_dir = (
            Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
            / "soloist"
        )
        return (data_dir / "cache" / "Users").is_dir()

    def pair(self) -> str:
        """Pair the device once, with a Spotify app on this network.

        Blocking: it waits (up to 10 minutes) for the pairing. On success
        returns the username; on a failure the user can act on, raises
        AuthenticationError with the end of what Soloist said.
        """
        if not self.supported:
            raise NonStreamableError(UNSUPPORTED_PLATFORM)
        binary = self.ensure_binary()
        proc = start_cmd(
            [str(binary), "-n", self.device_name, "-k", self.api_key, "-p"]
        )
        try:
            lines = _read_until_done(proc, timeout_s=600.0)
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.refused.clear()  # a new pairing is a new account
        _, _, username = summarize_log(self._redact(lines), "")
        if rc != 0 or username is None:
            raise AuthenticationError(
                f"Spotify did not accept the pairing (exit {rc}): {self._said(lines)}"
            )
        return username

    # --- the play and the record ---------------------------------------------------

    def estimate_size(self, duration_s: float) -> int:
        """A rough size in bytes of the FLAC that `duration_s` becomes: it
        only sizes the progress bar."""
        # 48 kHz, stereo, 16-bit, a little under the raw size (music compresses
        # near that).
        return int(duration_s * 48000 * 2 * 2 * 0.6)

    def play_and_capture(
        self, uri: str, duration_s: float, out_path: Path, report: Callable[[int], None]
    ) -> None:
        """Play `uri` with Soloist and record it as FLAC at out_path.

        Blocking, worker thread. Progress is the account's seconds against
        the FLAC's estimated size. A failure is a NonStreamableError (the
        youtube backend is the fallback), and the track is refused for the
        rest of the process; the one exception is an expired build (exit
        code 10), which earns a fresh build and one more try.
        """
        if not self.supported:
            raise NonStreamableError(UNSUPPORTED_PLATFORM)
        binary = self.ensure_binary()
        self.ensure_sink()
        try:
            self._play_once(binary, uri, duration_s, out_path, report)
        except _ExpiredBuildError:
            logger.info("The Soloist build has expired: downloading a fresh one")
            binary = self.refresh_binary()
            try:
                self._play_once(binary, uri, duration_s, out_path, report)
            except _ExpiredBuildError as e:
                # Even a fresh build is past its 90 days: the CDN is serving
                # the wrong one, and the track goes to the youtube backend.
                raise self._refuse(
                    uri, "even a fresh Soloist build will not play the track"
                ) from e

    def _refuse(self, uri: str, reason: str) -> NonStreamableError:
        """A track Spotify will not play: recorded, and not asked again."""
        self.refused.add(uri.rsplit(":", 1)[-1])
        return NonStreamableError(reason)

    def _play_once(
        self, binary: Path, uri: str, duration_s: float, out_path: Path, report
    ) -> None:
        """One full play-and-record: the recorder, Soloist, the trim, the
        FLAC, the check that it is the track.

        Nothing is left behind on any exit: the recorder is killed, the
        player stopped, and the capture removed, on a success, a failure, or
        a Ctrl-C. The FLAC is encoded before that cleanup deletes the
        capture, so the encoder always finds its input.
        """
        ffmpeg = require_ffmpeg()
        capture = (
            Path(tempfile.gettempdir()) / f"__streamrip_soloist_{time.time_ns()}.wav"
        )
        recorder = None
        player = None
        try:
            recorder = start_cmd(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "pulse",
                    "-i",
                    f"{SINK}.monitor",
                    "-c:a",
                    "pcm_f32le",
                    str(capture),
                ],
                quiet=True,
            )
            t_recorder = time.monotonic()
            time.sleep(2)
            player = start_cmd(
                [
                    str(binary),
                    "-n",
                    self.device_name,
                    "-k",
                    self.api_key,
                    "-d",
                    SINK,
                    "-i",
                    "100",
                    "-s",
                    uri,
                ]
            )
            lines, t_playing = self._follow(player, uri, duration_s, report)
            rc = player.wait()
            if rc == EXPIRED_BUILD:
                # The thing that refused is the build, not the track.
                raise _ExpiredBuildError
            if rc != 0:
                raise self._refuse(
                    uri,
                    f"Soloist did not play the track (exit {rc}): {self._said(lines)}",
                )
            started, _, _ = summarize_log(self._redact(lines), uri)
            if not started:
                raise self._refuse(
                    uri, f"Soloist never began the track: {self._said(lines)}"
                )
            # The cut starts where "playing" came; the rest is pre-roll.
            offset = t_playing - t_recorder if t_playing is not None else None
            # Encode and check while the capture still exists: the cleanup in
            # the finally below removes it, and ffmpeg reads it. A refusal
            # here is the track's, so the partial FLAC goes with it.
            try:
                self._encode(ffmpeg, capture, out_path, offset, duration_s)
                self._check(ffmpeg, out_path, duration_s)
            except NonStreamableError as e:
                out_path.unlink(missing_ok=True)
                raise self._refuse(uri, str(e)) from e
        finally:
            self._stop(recorder)
            if player is not None and player.poll() is None:
                player.kill()
                player.wait()
            capture.unlink(missing_ok=True)

    def _follow(
        self, player: subprocess.Popen, uri: str, duration_s: float, report
    ) -> tuple[list[str], float | None]:
        """Follow Soloist's lines until it finishes, or gives up.

        Returns its lines and, when the track began, the estimated clock
        moment of that: the first "playing" line's clock, minus the position
        it reported (a first line that arrives late is already into the
        track). The position of a playing line is the progress. A run that
        never began within PLAYING_WAIT, or that stops well before the track
        ends, is a refusal.
        """
        estimate = self.estimate_size(duration_s)
        deadline = time.monotonic() + max(duration_s, 30.0) + PLAY_MARGIN
        not_started_until = time.monotonic() + PLAYING_WAIT
        pending = queue.Queue()

        def read():
            for line in player.stdout:
                pending.put(line)

        threading.Thread(target=read, daemon=True).start()
        lines: list[str] = []
        t_started = None
        while True:
            line = None
            with contextlib.suppress(queue.Empty):
                line = pending.get(timeout=0.5)
            if line is not None:
                line = line.rstrip("\n")
                lines.append(line)
                if t_started is None and is_playing_line(line, uri):
                    t_started = time.monotonic() - (line_position_s(line) or 0.0)
                elif (pos := line_position_s(line)) is not None and estimate:
                    report(int(min(1.0, pos / max(duration_s, 1.0)) * estimate))
            now = time.monotonic()
            if player.poll() is not None and pending.empty():
                break
            if t_started is None and now >= not_started_until:
                player.kill()
                raise self._refuse(
                    uri,
                    f"Soloist never began the track within {PLAYING_WAIT:.0f}s: "
                    f"{self._said(lines)}",
                )
            if now >= deadline:
                player.kill()
                raise self._refuse(
                    uri,
                    f"Soloist stopped before the track finished: {self._said(lines)}",
                )
        return lines, t_started

    @staticmethod
    def _stop(proc: subprocess.Popen | None) -> None:
        """Stop a recorder, if there is one, and wait for it to flush."""
        if proc is None:
            return
        with contextlib.suppress(subprocess.SubprocessError):
            proc.terminate()
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    @staticmethod
    def _encode(
        ffmpeg: str,
        capture: Path,
        out_path: Path,
        offset: float | None,
        duration_s: float,
    ) -> None:
        """The track, trimmed of its pre-roll, as FLAC at out_path.

        `offset` is the pre-roll, in seconds, from the recorder's start to
        the "playing" line; None is the end of the capture, from its last
        second, when the line never came.
        """
        cmd = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
        ]
        if offset is not None:
            cmd += [
                "-ss",
                f"{max(offset, 0.0):.3f}",
                "-t",
                f"{max(duration_s, 0.0):.3f}",
            ]
        else:
            cmd += ["-sseof", f"-{max(duration_s, 0.0):.3f}"]
        cmd += [
            "-i",
            str(capture),
            "-c:a",
            "flac",
            "-sample_fmt",
            "s16",
            "-compression_level",
            "5",
            str(out_path),
        ]
        result = run_cmd(cmd, timeout=max(duration_s, 60.0) + 120.0)
        if result.returncode != 0 or not out_path.is_file():
            out_path.unlink(missing_ok=True)
            said = result.stderr.strip()[-300:]
            raise NonStreamableError(
                f"ffmpeg failed to encode the FLAC (exit {result.returncode}): {said}"
            )

    @staticmethod
    def _check(ffmpeg: str, out_path: Path, duration_s: float) -> None:
        """The FLAC is the track: as long as it is, and it is not silence."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            raise NonStreamableError(
                "ffprobe is missing, and the FLAC cannot be checked"
            )
        probed = run_cmd(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(out_path),
            ]
        )
        try:
            got = float(probed.stdout.strip())
        except ValueError:
            got = -1.0
        if got <= 0 or abs(got - duration_s) > 1.5:
            raise NonStreamableError(
                f"the captured FLAC is {got:.1f}s, the track is {duration_s:.1f}s: "
                "it is not the track"
            )
        volume = run_cmd(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(out_path),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ]
        )
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", volume.stderr)
        if m is None or float(m.group(1)) <= -50.0:
            raise NonStreamableError("the captured FLAC is silent: it is not the track")

    # --- keeping the key out of what is said --------------------------------------

    def _redact(self, lines: list[str]) -> list[str]:
        """The output without the API key, wherever it may have been echoed."""
        if not self.api_key:
            return lines
        return [line.replace(self.api_key, "[soloist key]") for line in lines]

    def _said(self, lines: list[str]) -> str:
        """What a process said, at the end, without the API key in it."""
        return tail(self._redact(lines))
