"""Spotify's own audio with Soloist: the backend the audio comes from, its
play-and-record, the sign-in, and the fallback to the youtube backend when
it cannot serve a track."""

import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.client import soloist
from streamrip.client.audio_match import MatchTrack
from streamrip.client.downloadable import SoloistDownloadable, YtDlpDownloadable
from streamrip.client.soloist import (
    SoloistBackend,
    build_archive,
    is_playing_line,
    line_position_s,
    summarize_log,
    tail,
)
from streamrip.client.spotify import SpotifyClient
from streamrip.client.ytmusic import AudioMatch
from streamrip.config import Config
from streamrip.exceptions import AuthenticationError, NonStreamableError
from streamrip.media import track as track_module
from streamrip.media.track import Track
from streamrip.rip import prompter as prompter_module
from streamrip.rip.prompter import SpotifyPrompter

URI = "spotify:track:2A2b3C4d5E6f7G8h9I0jKl"
TRACK = URI.rsplit(":", 1)[-1]
PLAYING_LINES = [
    "restoring session",
    "logged in as stentijhuis861",
    f'playing {URI} "Sneaky Snitch" by "Kevin MacLeod" [0:05 / 2:15]',
    f'playing {URI} "Sneaky Snitch" by "Kevin MacLeod" [1:23 / 2:15]',
    "shutting down",
    "stopped",
]


def track_obj(**extra):
    return {
        "id": TRACK,
        "type": "track",
        "name": "Sneaky Snitch",
        "artists": [{"id": "a1", "name": "Kevin MacLeod"}],
        "album": {"id": "b1", "name": "Sneaky Snitch"},
        "duration_ms": 135_000,
        "track_number": 1,
        "disc_number": 1,
        "explicit": False,
        "external_ids": {"isrc": "usabc1100001"},
    } | extra


class FakeBackend:
    """A SoloistBackend stand-in: the refused set, a paired flag, and a
    play_and_capture that records its calls and answers as told."""

    def __init__(self, refused=(), play_error=None):
        self.refused = set(refused)
        self.device_name = "streamrip"
        self.api_key = ""
        self.paired_flag = True
        self.play_error = play_error
        self.plays = []

    def estimate_size(self, duration_s: float) -> int:
        return int(duration_s * 86_000)

    def play_and_capture(self, uri, duration_s, out_path, report):
        self.plays.append((uri, duration_s, str(out_path)))
        if self.play_error is not None:
            raise self.play_error
        Path(out_path).write_bytes(b"flac")
        report(86_000)

    def paired(self):
        return self.paired_flag

    def pair(self):
        return "user"


_OPENED: list[SpotifyClient] = []


@pytest.fixture(autouse=True)
async def _close_clients():
    yield
    for client in _OPENED:
        await client.session.close()
    _OPENED.clear()


def make_config(**options):
    config = Config.defaults()
    config.session.downloads.requests_per_minute = -1
    defaults = {
        "client_id": "CID",
        "refresh_token": "REFRESH",
        "access_token": "TOKEN",
        "token_expiry": str(int(time.time()) + 3600),
        "soloist_api_key": "KEY",
    }
    for name, value in (defaults | options).items():
        setattr(config.session.spotify, name, value)
    return config


def make_client(**options) -> SpotifyClient:
    """A logged-in client, with its session open (an async test's loop)."""
    client = SpotifyClient(make_config(**options))
    client._ensure_session()
    client.logged_in = True
    _OPENED.append(client)
    return client


class RecordingMatcher:
    def __init__(self, result):
        self.result = result
        self.asked = []

    async def find(self, wanted, isrc=None):
        self.asked.append((wanted, isrc))
        return self.result


# --- the backend the audio comes from ------------------------------------------


def test_the_backend_reads_the_key_and_device_name_from_the_config():
    backend = SoloistBackend(make_config(device_name="rip").session.spotify)
    assert backend.api_key == "KEY"
    assert backend.device_name == "rip"
    assert backend.estimate_size(1) > 0


async def test_the_backend_is_built_on_first_use_and_cached():
    client = make_client()
    assert isinstance(client.soloist_backend, SoloistBackend)
    assert client.soloist_backend is client.soloist_backend  # one per process
    assert client.backend_available is True


async def test_the_backend_is_none_where_it_cannot_serve():
    for client in (make_client(backend="youtube"), make_client(soloist_api_key="")):
        assert client.soloist_backend is None
        assert client.backend_available is False


async def test_pairing_stores_the_key_and_pairs_the_device(monkeypatch):
    client = make_client(soloist_api_key="")

    class FakeBuilt:
        def __init__(self, config):
            self.api_key = config.soloist_api_key

        def pair(self):
            assert self.api_key == "NEW"
            return "user"

    monkeypatch.setattr(soloist, "SoloistBackend", FakeBuilt)

    assert await client.pair_soloist("NEW") == "user"
    assert client.config.soloist_api_key == "NEW"


async def test_pairing_without_a_key_is_a_login_error():
    client = make_client(soloist_api_key="")
    with pytest.raises(AuthenticationError, match="Soloist API key"):
        await client.pair_soloist("")


# --- the backend: the audio of a track ------------------------------------------


async def test_the_audio_is_spotifys_own_when_the_backend_is_on():
    client = make_client()
    fake = FakeBackend()
    client._soloist_backend = fake
    client._tracks[TRACK] = track_obj()
    matcher = RecordingMatcher(None)
    client.matcher = matcher

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, SoloistDownloadable)
    assert downloadable.extension == "flac"
    assert downloadable.track_id == TRACK
    assert downloadable.duration_ms == 135_000
    assert await downloadable.size() > 0
    assert fake.plays == []  # played on download, not resolution
    assert matcher.asked == []  # the youtube backend was never asked
    assert client._tracks[TRACK]["album"]["container"] == "FLAC"


async def test_the_audio_is_the_youtube_match_when_the_backend_is_off():
    client = make_client(backend="youtube")
    client._tracks[TRACK] = track_obj()
    found = AudioMatch(MatchTrack("Sneaky Snitch", video_id="vid123"), 0.97)
    client.matcher = RecordingMatcher(found)

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, YtDlpDownloadable)
    assert downloadable.url == "https://music.youtube.com/watch?v=vid123"


async def test_the_audio_is_the_youtube_match_when_the_key_is_not_in_the_config():
    client = make_client(soloist_api_key="")
    client._tracks[TRACK] = track_obj()
    found = AudioMatch(MatchTrack("Sneaky Snitch", video_id="vid123"), 0.97)
    client.matcher = RecordingMatcher(found)

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, YtDlpDownloadable)
    assert downloadable.url == "https://music.youtube.com/watch?v=vid123"


async def test_a_track_spotify_refused_to_play_goes_to_youtube_music():
    client = make_client()
    client._soloist_backend = FakeBackend(refused={TRACK})
    client._tracks[TRACK] = track_obj()
    found = AudioMatch(MatchTrack("Sneaky Snitch", video_id="vid123"), 0.97)
    client.matcher = RecordingMatcher(found)

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, YtDlpDownloadable)
    assert downloadable.url == "https://music.youtube.com/watch?v=vid123"


async def test_a_refused_track_stays_refused_for_the_rest_of_the_run():
    """get_downloadable does not hand a refused track to the spotify backend
    a second time, even though the backend is otherwise fine."""
    client = make_client()
    client._soloist_backend = FakeBackend(refused={TRACK})
    client._tracks[TRACK] = track_obj()
    client.matcher = RecordingMatcher(None)

    with pytest.raises(NonStreamableError, match="No match"):
        await client.get_downloadable(TRACK, 0)
    with pytest.raises(NonStreamableError, match="No match"):
        await client.get_downloadable(TRACK, 0)
    # Both times only the youtube backend was tried.
    assert len(client.matcher.asked) == 2


async def test_the_download_plays_the_track_and_records_it(tmp_path):
    fake = FakeBackend()
    downloadable = SoloistDownloadable(fake, TRACK, 135_000)
    path = tmp_path / "track.flac"

    await downloadable.download(path, lambda n: None)

    [uri, duration_s, out] = fake.plays[0]
    assert uri == URI
    assert duration_s == pytest.approx(135.0)
    assert out == str(path)
    assert path.read_bytes() == b"flac"


async def test_a_refused_play_is_the_refusal_the_fallback_reads(tmp_path):
    fake = FakeBackend(play_error=NonStreamableError("Spotify did not play the track"))
    downloadable = SoloistDownloadable(fake, TRACK, 135_000)

    with pytest.raises(NonStreamableError, match="did not play"):
        await downloadable.download(tmp_path / "track.flac", lambda n: None)


# --- what a Soloist line says ----------------------------------------------------


def test_a_playing_line_is_one_that_names_the_track():
    line = 'playing spotify:track:abc "Numb" by "Linkin Park" [0:00 / 3:07]'
    assert is_playing_line(line, "spotify:track:abc")
    # A track whose id is a prefix of this one does not match.
    assert not is_playing_line(line, "spotify:track:ab")
    assert not is_playing_line("restoring session", "spotify:track:abc")
    # Soloist timestamps its lines; the marker is found mid-line.
    real = (
        '2026-11-17 00:17:43.429: playing spotify:track:abc "Numb" by '
        '"Linkin Park" [0:40 / 3:07]'
    )
    assert is_playing_line(real, "spotify:track:abc")
    assert not is_playing_line(real, "spotify:track:ab")


def test_the_position_of_a_playing_line_is_its_seconds():
    assert line_position_s("playing x [1:23 / 3:07]") == 83.0
    assert line_position_s("playing x [10:00 / 10:01]") == 600.0
    assert line_position_s("logged in as user") is None


def test_a_run_says_whether_it_began_how_far_and_whose_account_it_was():
    lines = [
        "2026-11-17 00:17:41.224: restoring session",
        "2026-11-17 00:17:41.465: logged in as stentijhuis861",
        (
            '2026-11-17 00:17:43.429: playing spotify:track:abc "Numb" by '
            '"Linkin Park" [0:00 / 3:07]'
        ),
        (
            '2026-11-17 00:19:06.428: playing spotify:track:abc "Numb" by '
            '"Linkin Park" [1:23 / 3:07]'
        ),
        "2026-11-17 00:20:49.612: stopped",
    ]
    assert summarize_log(lines, "spotify:track:abc") == (True, 83.0, "stentijhuis861")
    assert summarize_log(lines, "spotify:track:zzz") == (False, 83.0, "stentijhuis861")


def test_the_tail_is_the_end_of_what_was_said():
    assert tail(["a", "b", "c"]) == "a b c"
    assert tail([]) == "(it said nothing)"
    assert len(tail(["x" * 400])) <= 300


@pytest.mark.parametrize(
    ("platform", "machine", "archive"),
    [
        ("linux", "x86_64", "soloist_release_x86_64.tar.gz"),
        ("linux", "aarch64", "soloist_release_arm64.tar.gz"),
        ("win32", "AMD64", "soloist_release_windows_x86_64.zip"),
        ("darwin", "arm64", "soloist_release_osx_arm64.tar.gz"),
        ("darwin", "x86_64", None),
    ],
    ids=["linux x86_64", "linux aarch64", "windows", "macos arm64", "macos x86_64"],
)
def test_the_build_archive_of_each_platform(monkeypatch, platform, machine, archive):
    monkeypatch.setattr(soloist.sys, "platform", platform)
    monkeypatch.setattr(soloist.platform, "machine", lambda: machine)
    assert build_archive() == archive


def test_the_key_never_lands_in_the_words_streamrip_says():
    backend = SoloistBackend(make_config().session.spotify)
    key = backend.api_key
    lines = [f"-k {key} was echoed by the client"]
    assert key not in " ".join(backend._redact(lines))
    assert key not in backend._said(lines)


def test_the_sink_is_created_when_it_is_not_there(monkeypatch):
    backend = SoloistBackend(make_config().session.spotify)
    monkeypatch.setattr(
        soloist.shutil,
        "which",
        lambda name: "/usr/bin/pactl" if name == "pactl" else None,
    )
    ran = []

    def run_cmd(cmd, timeout=20):
        ran.append(cmd)
        if cmd[1:3] == ["list", "short"]:
            return subprocess.CompletedProcess([], 0, stdout="0 alsa_output desc\n")
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(soloist, "run_cmd", run_cmd)
    backend.ensure_sink()

    load = ran[-1]
    assert load[:4] == [
        "pactl",
        "load-module",
        "module-null-sink",
        f"sink_name={soloist.SINK}",
    ]


def test_an_existing_sink_is_not_created_again(monkeypatch):
    backend = SoloistBackend(make_config().session.spotify)
    monkeypatch.setattr(
        soloist.shutil,
        "which",
        lambda name: "/usr/bin/pactl" if name == "pactl" else None,
    )
    listed = subprocess.CompletedProcess(
        [], 0, stdout="0 alsa_output desc\n1 soloist_cap streamrip capture\n"
    )
    monkeypatch.setattr(soloist, "run_cmd", lambda cmd, timeout=20: listed)
    backend.ensure_sink()  # no load-module, no raise


def test_paired_is_the_stored_session(monkeypatch, tmp_path):
    backend = SoloistBackend(make_config().session.spotify)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert backend.paired() is False
    (tmp_path / "soloist" / "cache" / "Users").mkdir(parents=True)
    assert backend.paired() is True


# --- the play and the record -------------------------------------------------------


class FakeProcess:
    """A Popen: its lines on stdout, its exit code, and its calls counted."""

    def __init__(self, lines=(), rc=0):
        self.stdout = list(lines)
        self.rc = rc
        self.killed = False

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        return self.rc

    def terminate(self):
        pass

    def kill(self):
        self.killed = True


@pytest.fixture
def supported(monkeypatch):
    """The sink monitor is a Linux thing; the play tests do not depend on it."""
    monkeypatch.setattr(SoloistBackend, "supported", property(lambda self: True))


def play_env(monkeypatch, players, calls):
    """A SoloistBackend with its surroundings faked: the binary, the sink,
    the recorder, the players (in attempt order), the encode and the check.

    `calls` records what happened, in order ("refresh", "encode", "check").
    """
    backend = SoloistBackend(make_config().session.spotify)
    monkeypatch.setattr(backend, "ensure_binary", lambda: Path("/soloist"))
    monkeypatch.setattr(backend, "ensure_sink", lambda: None)
    monkeypatch.setattr(
        backend,
        "refresh_binary",
        lambda: calls.append("refresh") or Path("/soloist"),
    )
    monkeypatch.setattr(backend, "_encode", lambda *a: calls.append("encode"))
    monkeypatch.setattr(backend, "_check", lambda *a: calls.append("check"))
    monkeypatch.setattr(soloist, "require_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(time, "sleep", lambda s: None)

    players = iter(players)

    def start_cmd(cmd, quiet=False):
        return FakeProcess() if quiet else next(players)

    monkeypatch.setattr(soloist, "start_cmd", start_cmd)
    return backend


def test_a_played_track_is_recorded_cut_and_encoded(monkeypatch, supported, tmp_path):
    calls = []
    backend = play_env(monkeypatch, [FakeProcess(lines=PLAYING_LINES, rc=0)], calls)

    backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", lambda n: None)

    assert calls == ["encode", "check"]
    assert not backend.refused


def test_the_capture_still_exists_when_the_encode_runs(
    monkeypatch, supported, tmp_path
):
    """The WAV the recorder wrote must survive until the encode reads it: the
    second e2e run lost it (ffmpeg: 'No such file or directory') because the
    cleanup deleted the capture before the encode ran."""
    calls = []

    backend = SoloistBackend(make_config().session.spotify)
    monkeypatch.setattr(backend, "ensure_binary", lambda: Path("/soloist"))
    monkeypatch.setattr(backend, "ensure_sink", lambda: None)
    monkeypatch.setattr(soloist, "require_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def start_cmd(cmd, quiet=False):
        if quiet:
            # The recorder: its output, cmd[-1], is the capture file.
            Path(cmd[-1]).write_bytes(b"fake-wave")
            return FakeProcess(lines=[], rc=0)
        return FakeProcess(lines=PLAYING_LINES, rc=0)  # the player

    monkeypatch.setattr(soloist, "start_cmd", start_cmd)

    def encode(ffmpeg, capture, out_path, offset, duration_s):
        assert capture.is_file(), "the capture was removed before the encode"
        calls.append("encode")

    monkeypatch.setattr(backend, "_encode", encode)
    monkeypatch.setattr(backend, "_check", lambda *a: calls.append("check"))

    backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", lambda n: None)

    assert calls == ["encode", "check"]
    assert not backend.refused


def test_the_encode_writes_a_flac_this_ffmpeg_accepts(tmp_path):
    """The real encode must produce a FLAC this ffmpeg can actually write:
    its native flac encoder only takes s16/s32, so an s24 is refused with
    'Invalid sample format' (which the third e2e run hit)."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe not available")

    capture = tmp_path / "capture.wav"
    # A short 48 kHz stereo capture, the way the recorder writes one.
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            str(capture),
        ],
        check=True,
    )

    out = tmp_path / "out.flac"
    SoloistBackend._encode(ffmpeg, capture, out, 0.0, 2.0)

    assert out.is_file()
    probed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name",
            "-of",
            "default=nw=1",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "flac" in probed.stdout


def test_an_expired_build_is_refreshed_and_retried_once(
    monkeypatch, supported, tmp_path
):
    calls = []
    players = [
        FakeProcess(lines=[], rc=soloist.EXPIRED_BUILD),
        FakeProcess(lines=PLAYING_LINES, rc=0),
    ]
    backend = play_env(monkeypatch, players, calls)

    backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", lambda n: None)

    assert calls == ["refresh", "encode", "check"]
    assert not backend.refused


def test_an_expired_build_twice_is_a_refusal(monkeypatch, supported, tmp_path):
    """One refresh and one more try, not a loop: after that the track goes
    to the youtube backend."""
    calls = []
    players = [
        FakeProcess(lines=[], rc=soloist.EXPIRED_BUILD),
        FakeProcess(lines=[], rc=soloist.EXPIRED_BUILD),
    ]
    backend = play_env(monkeypatch, players, calls)

    with pytest.raises(NonStreamableError, match="will not play"):
        backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", lambda n: None)

    assert calls == ["refresh"]
    assert backend.refused == {TRACK}


def test_a_play_spotify_refused_is_refused(monkeypatch, supported, tmp_path):
    calls = []
    backend = play_env(monkeypatch, [FakeProcess(lines=["stopped"], rc=1)], calls)

    with pytest.raises(NonStreamableError, match="exit 1"):
        backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", lambda n: None)

    assert calls == []
    assert backend.refused == {TRACK}


def test_a_play_that_never_begins_is_refused(monkeypatch, supported, tmp_path):
    calls = []
    lines = ["restoring session", "logged in as stentijhuis861", "stopped"]
    backend = play_env(monkeypatch, [FakeProcess(lines=lines, rc=0)], calls)

    with pytest.raises(NonStreamableError, match="never began"):
        backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", lambda n: None)

    assert calls == []
    assert TRACK in backend.refused


def test_a_capture_that_fails_the_check_is_refused(monkeypatch, supported, tmp_path):
    calls = []
    backend = play_env(monkeypatch, [FakeProcess(lines=PLAYING_LINES, rc=0)], calls)

    def check(*a):
        raise NonStreamableError("the captured FLAC is silent: it is not the track")

    backend._check = check

    with pytest.raises(NonStreamableError, match="silent"):
        backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", lambda n: None)

    assert calls == ["encode"]
    assert TRACK in backend.refused


def test_an_unsupported_platform_never_plays(monkeypatch, tmp_path):
    monkeypatch.setattr(SoloistBackend, "supported", property(lambda self: False))
    backend = SoloistBackend(make_config().session.spotify)

    with pytest.raises(NonStreamableError, match="not supported on this platform"):
        backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", lambda n: None)


# --- the pairing -------------------------------------------------------------------


def test_pairing_reads_the_username_and_clears_the_refusals(monkeypatch):
    backend = SoloistBackend(make_config().session.spotify)
    backend.refused.add("another")
    monkeypatch.setattr(backend, "ensure_binary", lambda: Path("/soloist"))
    lines = [
        "running",
        "logged in as stentijhuis861",
        "pairing complete, credentials stored",
    ]
    monkeypatch.setattr(
        soloist, "start_cmd", lambda cmd, quiet=False: FakeProcess(lines=lines, rc=0)
    )

    assert backend.pair() == "stentijhuis861"
    assert backend.refused == set()


def test_a_pairing_spotify_declined_is_a_login_error(monkeypatch):
    backend = SoloistBackend(make_config().session.spotify)
    monkeypatch.setattr(backend, "ensure_binary", lambda: Path("/soloist"))
    monkeypatch.setattr(
        soloist,
        "start_cmd",
        lambda cmd, quiet=False: FakeProcess(lines=["stopped"], rc=1),
    )

    with pytest.raises(AuthenticationError, match="did not accept the pairing"):
        backend.pair()


def test_pairing_needs_the_sink_platform(monkeypatch):
    monkeypatch.setattr(SoloistBackend, "supported", property(lambda self: False))
    backend = SoloistBackend(make_config().session.spotify)

    with pytest.raises(NonStreamableError, match="not supported on this platform"):
        backend.pair()


# --- the fallback in the download loop -----------------------------------------------


def _track(
    downloadable, client, tmp_path, backend="spotify", source="spotify"
) -> Track:
    config = Config.defaults()
    config.session.spotify.backend = backend
    config.session.cli.progress_bars = False
    meta = MagicMock()
    meta.title = "T"
    meta.tracknumber = 1
    meta.info.id = "1"
    meta.album.info.container = "FLAC"
    meta.album.info.bit_depth = None
    meta.album.info.sampling_rate = None
    meta.album.albumartist = "A"
    return Track(
        meta,
        downloadable,
        config,
        tmp_path,
        None,
        None,
        client=client,
        download_path=tmp_path / "track.flac",
    )


def _refused_downloadable():
    refused = MagicMock()
    refused.url_expires = False
    refused.source = "spotify"
    refused.size = AsyncMock(return_value=0)
    refused.download = AsyncMock(
        side_effect=NonStreamableError("Spotify refused the track")
    )
    return refused


def _fresh_downloadable(download=None):
    fresh = MagicMock()
    fresh.url_expires = False
    fresh.source = "spotify"
    fresh.size = AsyncMock(return_value=0)
    # The youtube backend's bytes: an m4a, not the flac the name may say.
    fresh.extension = "m4a"
    if download is None:
        # A real download writes the part file; the loop moves it into place.
        download = AsyncMock(
            side_effect=lambda path, callback: path.write_bytes(b"audio")
        )
    fresh.download = download
    return fresh


@contextmanager
def _download_env():
    with (
        patch("streamrip.media.track.asyncio.sleep", new=AsyncMock()),
        patch("streamrip.media.track.global_download_semaphore", lambda c: _Slots()),
        patch(
            "streamrip.media.track.get_progress_callback", lambda *a, **k: _Progress()
        ),
    ):
        yield


class _Slots:
    """An async context manager: Track.download() holds it per attempt."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Progress:
    """A sync context manager yielding the callback: Track.download() uses it
    around the download."""

    def __enter__(self):
        return lambda n: None

    def __exit__(self, *args):
        return False


async def test_a_refused_track_is_re_downloaded_from_youtube_music(tmp_path):
    """Spotify refuses the track; the track is re-resolved on the youtube
    backend and downloaded from there."""
    fresh = _fresh_downloadable()
    client = MagicMock()
    client.source = "spotify"
    client.get_downloadable = AsyncMock(return_value=fresh)
    track = _track(_refused_downloadable(), client, tmp_path)

    with _download_env():
        await track.download()

    client.get_downloadable.assert_awaited_once_with("1", 0)
    fresh.download.assert_awaited_once()
    assert track.downloadable is fresh


async def test_the_fallback_happens_once(tmp_path):
    """A failure of the youtube backend is not re-routed: the track fails."""
    fresh = _fresh_downloadable(
        download=AsyncMock(side_effect=NonStreamableError("still not streamable"))
    )
    client = MagicMock()
    client.source = "spotify"
    client.get_downloadable = AsyncMock(return_value=fresh)
    track = _track(_refused_downloadable(), client, tmp_path)

    with _download_env(), pytest.raises(track_module.TrackDownloadFailedError):
        await track.download()

    assert client.get_downloadable.await_count == 1


@pytest.mark.parametrize(
    ("source", "backend"),
    [("tidal", "spotify"), ("spotify", "youtube")],
    ids=["another source", "the youtube backend"],
)
async def test_the_fallback_needs_the_spotify_client_and_backend(
    tmp_path, source, backend
):
    """A NonStreamableError from another source, or from the youtube backend
    itself, is not re-routed."""
    client = MagicMock()
    client.source = source
    client.get_downloadable = AsyncMock(return_value=_fresh_downloadable())
    track = _track(
        _refused_downloadable(), client, tmp_path, backend=backend, source=source
    )

    with _download_env(), pytest.raises(track_module.TrackDownloadFailedError):
        await track.download()

    client.get_downloadable.assert_not_awaited()


# --- the prompter -------------------------------------------------------------------


@pytest.fixture
def prompter(monkeypatch):
    """A prompter with a saved app and a client that only pretends to pair."""
    config = Config.defaults()
    config.session.spotify.client_id = "saved"
    client = SpotifyClient(config)
    client.pair_soloist = AsyncMock(return_value="user")
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    return SpotifyPrompter(config, client)


async def test_the_prompter_pairs_the_device_when_the_user_agrees(
    prompter, monkeypatch
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompter.config.session.spotify.soloist_api_key = "KEY"
    prompter.client._soloist_backend = FakeBackend()
    monkeypatch.setattr(
        prompter_module.Confirm, "ask", staticmethod(lambda *a, **k: True)
    )

    await prompter._offer_spotify_backend()

    prompter.client.pair_soloist.assert_awaited_once_with("KEY")
    assert prompter.config.session.spotify.soloist_api_key == "KEY"


async def test_the_prompter_asks_for_the_key_when_it_is_not_in_the_config(
    prompter, monkeypatch
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompter.client._soloist_backend = FakeBackend()
    asked = []

    def ask(prompt, password=False, **_):
        asked.append((prompt, password))
        return "NEW-KEY"

    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(ask))
    monkeypatch.setattr(
        prompter_module.Confirm, "ask", staticmethod(lambda *a, **k: True)
    )

    await prompter._offer_spotify_backend()

    assert asked[0] == ("Enter the Soloist API key", True)
    prompter.client.pair_soloist.assert_awaited_once_with("NEW-KEY")
    assert prompter.config.session.spotify.soloist_api_key == "NEW-KEY"


async def test_the_prompter_says_the_pairing_steps_when_the_device_is_new(
    prompter, monkeypatch
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompter.config.session.spotify.soloist_api_key = "KEY"
    fake = FakeBackend()
    fake.paired_flag = False
    prompter.client._soloist_backend = fake
    monkeypatch.setattr(
        prompter_module.Confirm, "ask", staticmethod(lambda *a, **k: True)
    )
    said = []
    monkeypatch.setattr(
        prompter_module.console, "print", lambda *a, **k: said.append(a[0])
    )

    await prompter._offer_spotify_backend()

    assert any("Pair this device" in s and "streamrip" in s for s in said)


async def test_the_prompter_does_not_offer_the_backend_when_it_is_off(
    prompter, monkeypatch
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompter.config.session.spotify.backend = "youtube"

    await prompter._offer_spotify_backend()

    prompter.client.pair_soloist.assert_not_awaited()


async def test_the_prompter_swallows_a_failed_pairing(prompter, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompter.config.session.spotify.soloist_api_key = "KEY"
    prompter.client._soloist_backend = FakeBackend()
    prompter.client.pair_soloist = AsyncMock(
        side_effect=AuthenticationError("declined")
    )
    monkeypatch.setattr(
        prompter_module.Confirm, "ask", staticmethod(lambda *a, **k: True)
    )

    await prompter._offer_spotify_backend()  # no raise

    prompter.client.pair_soloist.assert_awaited_once_with("KEY")


def test_the_key_saves_with_the_login(prompter):
    prompter.config.session.spotify.soloist_api_key = "KEY"

    prompter.save()

    assert prompter.config.file.spotify.soloist_api_key == "KEY"
