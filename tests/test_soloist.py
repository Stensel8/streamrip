"""Spotify's own audio with Soloist: playing and recording a track, the
pairing, and the fallback to YouTube Music when Spotify will not play it."""

import asyncio
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from time import sleep as real_sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mutagen.flac import FLAC

from streamrip.client import soloist
from streamrip.client.audio_match import MatchTrack
from streamrip.client.downloadable import SoloistDownloadable, YtDlpDownloadable
from streamrip.client.soloist import (
    SoloistBackend,
    build_archive,
    is_playing_line,
    line_position_s,
    summarize_log,
)
from streamrip.client.spotify import SpotifyClient
from streamrip.client.ytmusic import AudioMatch
from streamrip.config import Config
from streamrip.exceptions import AuthenticationError, NonStreamableError
from streamrip.media import track as track_module
from streamrip.media.track import Track
from streamrip.metadata.album import AlbumMetadata
from streamrip.rip import prompter as prompter_module
from streamrip.rip.prompter import SpotifyPrompter

URI = "spotify:track:2A2b3C4d5E6f7G8h9I0jKl"
TRACK = URI.rsplit(":", 1)[-1]
PLAYING_LINES = [
    "restoring session",
    "logged in as someone",
    f'playing {URI} "Sneaky Snitch" by "Kevin MacLeod" [0:05 / 2:15]',
    f'playing {URI} "Sneaky Snitch" by "Kevin MacLeod" [1:23 / 2:15]',
    "stopped",
]
YOUTUBE = AudioMatch(MatchTrack("Sneaky Snitch", video_id="vid123"), 0.97)


def track_obj():
    return {
        "id": TRACK,
        "type": "track",
        "name": "Sneaky Snitch",
        "artists": [{"id": "a1", "name": "Kevin MacLeod"}],
        "album": {"id": "b1", "name": "Sneaky Snitch"},
        "duration_ms": 135_000,
        "external_ids": {"isrc": "usabc1100001"},
    }


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


@pytest.fixture
async def make_client():
    """A logged-in client with its session open, for the options given."""
    clients = []

    def make(**options) -> SpotifyClient:
        client = SpotifyClient(make_config(**options))
        client._ensure_session()
        client.logged_in = True
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.session.close()


class FakeBackend:
    """A SoloistBackend that plays at once, and writes the uri as the audio."""

    turn = nullcontext()

    def __init__(self, refused=()):
        self.refused = set(refused)

    def estimate_size(self, duration_s):
        return int(duration_s * 86_000)

    def play_and_capture(self, uri, duration_s, out_path, report, stop=None):
        Path(out_path).write_text(uri)
        report(86_000)

    def paired(self):
        return True


async def test_the_audio_is_spotifys_own_when_the_backend_is_on(make_client):
    client = make_client()
    client._soloist_backend = FakeBackend()
    client._tracks[TRACK] = track_obj()
    client.matcher = SimpleNamespace(find=AsyncMock())

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, SoloistDownloadable)
    assert (downloadable.extension, downloadable.duration_ms) == ("flac", 135_000)
    assert client._tracks[TRACK]["album"]["container"] == "FLAC"
    client.matcher.find.assert_not_awaited()


async def test_an_album_from_spotifys_own_audio_says_what_its_files_are(make_client):
    client = make_client()
    client._soloist_backend = FakeBackend()
    album = {"id": "1", "name": "Album", "tracks": [], **client._audio()}

    meta = AlbumMetadata.from_spotify(album).at_quality(
        0, "spotify"
    )  # no tier lowers it

    folder = meta.format_folder_path(
        "{title} [{container}] [{bit_depth}B-{sampling_rate}kHz]"
    )
    assert folder.name == "Album [FLAC] [24B-44.1kHz]"


async def test_a_computer_that_cannot_record_goes_to_youtube_music_without_trying(
    make_client, monkeypatch
):
    # A container: Soloist would be fetched, fail, and the album would be named FLAC.
    monkeypatch.setattr(SoloistBackend, "supported", property(lambda self: False))
    client = make_client()

    assert client.soloist_backend is None
    assert client.container != "FLAC"
    assert client._audio() == {"container": client.container}


async def test_an_album_is_named_flac_when_spotifys_own_audio_is_on(make_client):
    # The folder and the progress label are made before the audio is asked for.
    client = make_client()
    client._soloist_backend = FakeBackend()

    assert client.container == "FLAC"


@pytest.mark.parametrize(
    "options",
    [{"backend": "youtube"}, {"soloist_api_key": ""}],
    ids=["backend off", "no key"],
)
async def test_the_audio_is_the_youtube_match_when_the_backend_cannot_serve(
    make_client, options
):
    client = make_client(**options)
    client._tracks[TRACK] = track_obj()
    client.matcher = SimpleNamespace(find=AsyncMock(return_value=YOUTUBE))

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, YtDlpDownloadable)
    assert downloadable.url == "https://music.youtube.com/watch?v=vid123"


async def test_a_track_spotify_refused_to_play_goes_to_youtube_music(make_client):
    client = make_client()
    client._soloist_backend = FakeBackend(refused={TRACK})
    client._tracks[TRACK] = track_obj()
    client.matcher = SimpleNamespace(find=AsyncMock(return_value=YOUTUBE))

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, YtDlpDownloadable)


async def test_the_fallback_is_youtube_music_for_a_track_spotify_did_not_refuse(
    make_client,
):
    # The sink or a busy session failed, not the track: get_downloadable plays it again.
    client = make_client()
    client._soloist_backend = FakeBackend()
    client._tracks[TRACK] = track_obj()
    client.matcher = SimpleNamespace(find=AsyncMock(return_value=YOUTUBE))

    assert isinstance(await client.youtube_downloadable(TRACK), YtDlpDownloadable)


async def test_the_download_plays_the_track_into_the_file(tmp_path):
    downloadable = SoloistDownloadable(FakeBackend(), TRACK, 135_000)

    await downloadable.download(tmp_path / "track.flac", lambda n: None)

    assert (tmp_path / "track.flac").read_text() == URI


async def test_a_cancelled_download_waits_for_the_play_and_leaves_no_file(tmp_path):
    started, ended = threading.Event(), threading.Event()

    class Playing(FakeBackend):
        def play_and_capture(self, uri, duration_s, out_path, report, stop=None):
            started.set()
            try:
                for _ in range(100):  # a second of real time, unless stopped
                    if stop is not None and stop.is_set():
                        raise asyncio.CancelledError
                    real_sleep(0.01)
                super().play_and_capture(uri, duration_s, out_path, report)
            finally:
                ended.set()

    path = tmp_path / "track.part.flac"
    download = asyncio.ensure_future(
        SoloistDownloadable(Playing(), TRACK, 135_000).download(path, lambda n: None)
    )
    await asyncio.to_thread(started.wait, 5)
    download.cancel()  # Ctrl-C, or a SIGTERM to streamrip alone

    with pytest.raises(asyncio.CancelledError):
        await download

    assert ended.is_set()  # stopped before the download gave the file up
    assert not path.exists()


async def test_pairing_stores_the_key_and_pairs_the_device(make_client, monkeypatch):
    client = make_client(soloist_api_key="")
    monkeypatch.setattr(SoloistBackend, "supported", property(lambda self: True))
    monkeypatch.setattr(SoloistBackend, "pair", lambda self: f"user of {self.api_key}")

    assert await client.pair_soloist("NEW") == "user of NEW"
    assert client.config.soloist_api_key == "NEW"


async def test_pairing_where_nothing_can_be_recorded_says_so(make_client, monkeypatch):
    monkeypatch.setattr(SoloistBackend, "supported", property(lambda self: False))

    with pytest.raises(AuthenticationError, match="cannot record"):
        await make_client(soloist_api_key="").pair_soloist("KEY")


async def test_pairing_without_a_key_is_a_login_error(make_client):
    with pytest.raises(AuthenticationError, match="Soloist API key"):
        await make_client(soloist_api_key="").pair_soloist("")


def test_a_playing_line_is_one_that_names_the_track():
    line = (
        '2026-11-17 00:17:43.429: playing spotify:track:abc "Numb" by '
        '"Linkin Park" [0:40 / 3:07]'
    )

    assert is_playing_line(line, "spotify:track:abc")
    assert not is_playing_line(line, "spotify:track:ab")  # another track
    assert not is_playing_line("restoring session", "spotify:track:abc")


def test_the_position_of_a_playing_line_is_its_seconds():
    assert line_position_s("playing x [1:23 / 3:07]") == 83.0
    assert line_position_s("logged in as user") is None


def test_a_run_says_whether_it_began_how_far_and_whose_account_it_was():
    assert summarize_log(PLAYING_LINES, URI) == (True, 83.0, "someone")
    assert summarize_log(PLAYING_LINES, "spotify:track:zzz") == (False, 83.0, "someone")


@pytest.mark.parametrize(
    ("platform", "machine", "archive"),
    [
        ("linux", "x86_64", "soloist_release_x86_64.tar.gz"),
        ("win32", "AMD64", "soloist_release_windows_x86_64.zip"),
        ("darwin", "x86_64", None),  # there is no build for an Intel Mac
    ],
)
def test_the_build_archive_of_each_platform(monkeypatch, platform, machine, archive):
    monkeypatch.setattr(soloist.sys, "platform", platform)
    monkeypatch.setattr(soloist.platform, "machine", lambda: machine)

    assert build_archive() == archive


def test_the_key_never_lands_in_the_words_streamrip_says():
    backend = SoloistBackend(make_config().session.spotify)
    lines = [f"-k {backend.api_key} was echoed by the client"]

    assert backend.api_key not in backend._said(lines)


def test_a_device_is_paired_once_a_session_is_stored(monkeypatch, tmp_path):
    backend = SoloistBackend(make_config().session.spotify)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert backend.paired() is False

    (tmp_path / "soloist" / "cache" / "Users").mkdir(parents=True)

    assert backend.paired() is True


def test_the_encode_writes_a_24_bit_flac_at_spotifys_rate(tmp_path):
    # Its flac encoder only takes s16/s32: an s24 was refused ("Invalid sample format").
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not available")
    capture = tmp_path / "capture.raw"
    # A short capture, the way the recorder writes one: raw 32-bit float at 44.1 kHz.
    args = ["-f", "lavfi", "-i", "sine=duration=2", "-ar", "44100", "-ac", "2"]
    subprocess.run(
        [ffmpeg, "-loglevel", "error", *args, "-f", "f32le", str(capture)], check=True
    )
    out = tmp_path / "out.flac"

    SoloistBackend._encode(ffmpeg, capture, out, 0.0, 2.0)

    info = FLAC(out).info
    assert (info.bits_per_sample, info.sample_rate) == (24, 44100)
    assert info.length == pytest.approx(2.0, abs=0.2)


def test_the_track_is_cut_at_its_first_sound_not_at_the_line_that_says_it_plays(
    tmp_path,
):
    import array

    capture = tmp_path / "capture.raw"
    # 1 s of the sink's silence, then a sound, in stereo 32-bit floats.
    silence = array.array("f", [0.0] * (2 * soloist.RATE))
    sound = array.array("f", [0.5, 0.5] * 100)
    capture.write_bytes(silence.tobytes() + sound.tobytes())

    found = SoloistBackend._first_sound(capture, earliest=0.5)

    assert found == pytest.approx(1.0, abs=1 / soloist.RATE)
    assert (
        SoloistBackend._first_sound(capture, earliest=0.0) == found
    )  # the same from before


def test_a_silence_that_lasts_longer_than_the_search_leaves_the_cut_alone(tmp_path):
    import array

    capture = tmp_path / "capture.raw"
    capture.write_bytes(array.array("f", [0.0] * (2 * 4 * soloist.RATE)).tobytes())

    assert SoloistBackend._first_sound(capture, earliest=0.0) is None


def test_the_prefs_ask_for_lossless_without_normalization(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    prefs = tmp_path / "soloist" / "settings" / "Users" / "someone-user" / "prefs"
    prefs.parent.mkdir(parents=True)
    prefs.write_text("audio.gapless_migrated_to_ps=true\naudio.normalize_v2=true\n")

    backend = SoloistBackend(make_config().session.spotify)
    backend.set_prefs()
    backend.set_prefs()  # written again before every play: nothing piles up

    assert prefs.read_text().splitlines() == [
        "audio.gapless_migrated_to_ps=true",
        "audio.play_bitrate_enumeration=5",
        "audio.play_bitrate_non_metered_enumeration=5",
        "audio.normalize_v2=false",
    ]


def process(lines=(), rc=0):
    """A Popen: its lines on stdout and its exit code."""
    return SimpleNamespace(
        stdout=list(lines),
        poll=lambda: rc,
        wait=lambda timeout=None: rc,
        terminate=lambda: None,
        kill=lambda: None,
    )


@pytest.fixture
def backend(monkeypatch):
    """A SoloistBackend with its surroundings faked; `calls` says what ran."""
    backend = SoloistBackend(make_config().session.spotify)
    backend.calls = []

    def encode(ffmpeg, capture, *_):
        assert capture.is_file(), "the capture was removed before the encode"
        backend.calls.append("encode")

    def refresh():
        backend.calls.append("refresh")
        return Path("/soloist")

    monkeypatch.setattr(SoloistBackend, "supported", property(lambda self: True))
    monkeypatch.setattr(backend, "ensure_binary", lambda: Path("/soloist"))
    monkeypatch.setattr(backend, "ensure_sink", lambda: None)
    monkeypatch.setattr(backend, "set_prefs", lambda: None)
    monkeypatch.setattr(backend, "refresh_binary", refresh)
    monkeypatch.setattr(backend, "_encode", encode)
    monkeypatch.setattr(backend, "_check", lambda *a: backend.calls.append("check"))
    monkeypatch.setattr(soloist, "require_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(soloist, "BUSY_WAIT", 0)
    return backend


@pytest.fixture
def play(backend, monkeypatch, tmp_path):
    """Plays a track on the backend; the players are given in attempt order."""

    def play(*players, report=lambda n: None, stop=None):
        players = iter(players)

        def start_cmd(cmd, quiet=False):
            if quiet:  # the recorder: its last argument is the capture file
                Path(cmd[-1]).write_bytes(b"wave")
                return process()
            return next(players)

        monkeypatch.setattr(soloist, "start_cmd", start_cmd)
        backend.play_and_capture(URI, 135.0, tmp_path / "track.flac", report, stop)

    return play


def test_a_played_track_is_recorded_and_encoded(play, backend):
    play(process(PLAYING_LINES))

    assert backend.calls == ["encode", "check"]
    assert not backend.refused


def test_a_track_spotify_did_not_play_as_flac_is_said(play, caplog):
    play(process([*PLAYING_LINES, "08:40:36.225 I [f:347 ] codec: vorbis"]))

    assert "played 'Sneaky Snitch' as vorbis" in caplog.text


async def test_the_tracks_of_an_album_are_played_one_at_a_time(backend, tmp_path):
    # Five at once (max_connections) made four of them fail with "another session
    # is running", and the failures went to YouTube Music.
    playing = most = 0

    def play_once(*_):
        nonlocal playing, most
        playing += 1
        most = max(most, playing)
        real_sleep(0.05)  # the fixture's time.sleep does nothing
        playing -= 1

    backend._play_once = play_once
    tracks = [SoloistDownloadable(backend, f"id{i}", 1000) for i in range(5)]

    await asyncio.gather(
        *(
            t.download(tmp_path / f"{i}.flac", lambda n: None)
            for i, t in enumerate(tracks)
        )
    )

    assert most == 1


BUSY_LINES = ['Error: another session is running for data directory "/x". Stop it.']


def test_a_busy_soloist_is_waited_for_not_refused(play, backend):
    play(process(BUSY_LINES, rc=1), process(PLAYING_LINES))

    assert backend.calls == ["encode", "check"]
    assert not backend.refused


def test_a_soloist_that_stays_busy_gives_the_track_to_youtube_not_a_refusal(
    play, backend, monkeypatch
):
    monkeypatch.setattr(soloist, "BUSY_TRIES", 2)

    with pytest.raises(NonStreamableError, match="stayed busy"):
        play(process(BUSY_LINES, rc=1), process(BUSY_LINES, rc=1))

    assert not backend.refused


def test_the_progress_advances_by_what_is_new_and_leaves_room_for_the_encode(
    play, backend
):
    # A progress bar adds what it is given: the whole position so far, each
    # time, ran it to 100% a minute into the track.
    reports = []
    lines = [
        f'playing {URI} "Song" by "Band" [{m}:{s:02d} / 2:15]'
        for m, s in [(0, 0), (0, 10), (0, 20), (1, 10), (2, 10)]
    ]

    play(process(lines), report=reports.append)

    expected = backend.estimate_size(135.0) * soloist.PLAY_SHARE * 130 / 135
    assert all(n > 0 for n in reports)
    assert sum(reports) == pytest.approx(expected, rel=0.03)


async def test_the_tracks_of_an_album_wait_in_line_with_no_row_and_no_slot(
    refused_track, monkeypatch
):
    rows = most = 0

    @contextmanager
    def row(*_, **__):
        nonlocal rows, most
        rows += 1
        most = max(most, rows)
        try:
            yield lambda n: None
        finally:
            rows -= 1

    monkeypatch.setattr("streamrip.media.track.get_progress_callback", row)
    line = asyncio.Lock()

    def waiting_track():
        track, *_ = refused_track()
        audio = MagicMock(url_expires=False, source="spotify", extension="flac")
        audio.queue = line
        audio.size = AsyncMock(return_value=0)

        async def download(path, callback):
            await asyncio.get_running_loop().run_in_executor(None, real_sleep, 0.02)
            path.write_bytes(b"audio")

        audio.download = download
        track.downloadable = audio
        return track

    await asyncio.gather(*(waiting_track().download() for _ in range(4)))

    assert most == 1


def test_an_expired_build_is_refreshed_and_retried_once(play, backend):
    play(process(rc=soloist.EXPIRED_BUILD), process(PLAYING_LINES))

    assert backend.calls == ["refresh", "encode", "check"]
    assert not backend.refused


@pytest.mark.parametrize(
    ("lines", "rc", "message"),
    [
        (["stopped"], 1, "exit 1"),
        (["restoring session", "logged in as someone", "stopped"], 0, "never began"),
    ],
    ids=["refused", "never began"],
)
def test_a_play_that_fails_is_refused(play, backend, lines, rc, message):
    with pytest.raises(NonStreamableError, match=message):
        play(process(lines, rc))

    assert backend.refused == {TRACK}
    assert backend.calls == []


def test_a_track_spotify_refused_is_not_played_again(play, backend):
    backend.refused.add(TRACK)

    with pytest.raises(NonStreamableError, match="already refused"):
        play()  # no player: nothing may start

    assert backend.calls == []


@pytest.mark.parametrize("busy", [False, True], ids=["playing", "waiting its turn"])
def test_a_cancelled_play_stops_and_refuses_nothing(play, backend, busy):
    stop = threading.Event()
    player = process(BUSY_LINES if busy else PLAYING_LINES, rc=1 if busy else 0)
    if busy:  # cancelled while it waits for the other session
        player.wait = lambda timeout=None: stop.set() or 1
    else:
        stop.set()

    with pytest.raises(asyncio.CancelledError):
        play(player, stop=stop)

    assert not backend.refused
    assert backend.calls == []


def test_a_capture_that_fails_the_check_is_refused(play, backend):
    def silent(*_):
        raise NonStreamableError("the captured FLAC is silent: it is not the track")

    backend._check = silent

    with pytest.raises(NonStreamableError, match="silent"):
        play(process(PLAYING_LINES))

    assert backend.refused == {TRACK}


def test_an_unsupported_platform_never_plays(monkeypatch, tmp_path):
    monkeypatch.setattr(SoloistBackend, "supported", property(lambda self: False))
    backend = SoloistBackend(make_config().session.spotify)

    with pytest.raises(NonStreamableError, match="not supported on this platform"):
        backend.play_and_capture(URI, 135.0, tmp_path / "t.flac", lambda n: None)


def test_pairing_reads_the_username_and_clears_the_refusals(monkeypatch, backend):
    backend.refused.add("another")
    lines = ["running", "logged in as someone", "pairing complete"]
    monkeypatch.setattr(soloist, "start_cmd", lambda cmd, quiet=False: process(lines))

    assert backend.pair() == "someone"
    assert backend.refused == set()


def test_a_pairing_spotify_declined_is_a_login_error(monkeypatch, backend):
    monkeypatch.setattr(
        soloist, "start_cmd", lambda cmd, quiet=False: process(["stopped"], rc=1)
    )

    with pytest.raises(AuthenticationError, match="did not accept the pairing"):
        backend.pair()


@pytest.fixture
def refused_track(monkeypatch, tmp_path):
    """A track whose audio Spotify refuses, and a client that offers the
    YouTube Music audio instead: (track, client, that audio)."""
    monkeypatch.setattr("streamrip.media.track.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(
        "streamrip.media.track.global_download_semaphore", lambda c: nullcontext()
    )
    monkeypatch.setattr(
        "streamrip.media.track.get_progress_callback",
        lambda *a, **k: nullcontext(lambda n: None),
    )

    def make(
        source="spotify", backend="spotify", youtube_fails=False, youtube_missing=False
    ):
        config = Config.defaults()
        config.session.spotify.backend = backend
        config.session.cli.progress_bars = False
        meta = MagicMock()
        meta.info.id = "1"
        meta.album.info.bit_depth = meta.album.info.sampling_rate = None
        refused = MagicMock(url_expires=False, source="spotify")
        refused.size = AsyncMock(return_value=0)
        refused.download = AsyncMock(side_effect=NonStreamableError("refused"))
        youtube = MagicMock(url_expires=False, source="spotify", extension="m4a")
        youtube.size = AsyncMock(return_value=0)
        youtube.download = AsyncMock(
            side_effect=NonStreamableError("still not streamable")
            if youtube_fails
            else lambda path, callback: path.write_bytes(b"audio")
        )
        client = MagicMock(source=source)
        client.youtube_downloadable = AsyncMock(
            return_value=youtube,
            side_effect=NonStreamableError("No match on YouTube Music")
            if youtube_missing
            else None,
        )
        track = Track(
            meta,
            refused,
            config,
            tmp_path,
            None,
            None,
            client=client,
            download_path=tmp_path / "track.flac",
        )
        return track, client, youtube

    return make


async def test_a_track_soloist_fails_is_downloaded_again_from_youtube_music(
    refused_track,
):
    # Whatever failed (a refusal, the sink, a busy session): not Soloist again.
    track, client, youtube = refused_track()

    await track.download()

    client.youtube_downloadable.assert_awaited_once_with("1")
    assert track.downloadable is youtube


async def test_the_fallback_happens_once(refused_track):
    track, client, _ = refused_track(youtube_fails=True)

    with pytest.raises(track_module.TrackDownloadFailedError):
        await track.download()

    assert client.youtube_downloadable.await_count == 1


@pytest.mark.parametrize(
    ("errors", "youtube_missing", "plays"),
    [
        ([OSError("no capture")] * 3 + [NonStreamableError("refused")], False, 4),
        ([NonStreamableError("refused")] * 4, True, 1),
    ],
    ids=["refused on the last attempt", "nothing on YouTube Music"],
)
async def test_a_fallback_never_ends_as_a_success_nor_plays_again(
    refused_track, errors, youtube_missing, plays
):
    track, _, _ = refused_track(youtube_missing=youtube_missing)
    track.downloadable.download = soloist_play = AsyncMock(side_effect=errors)

    failed = pytest.raises(track_module.TrackDownloadFailedError)
    with failed if youtube_missing else nullcontext():
        await track.download()

    assert soloist_play.await_count == plays
    assert track.download_path.is_file() != youtube_missing


@pytest.mark.parametrize(
    "options",
    [{"source": "tidal"}, {"backend": "youtube"}],
    ids=["another source", "the youtube backend"],
)
async def test_the_fallback_is_for_the_spotify_backend_only(refused_track, options):
    track, client, _ = refused_track(**options)

    with pytest.raises(track_module.TrackDownloadFailedError):
        await track.download()

    client.youtube_downloadable.assert_not_awaited()


@pytest.fixture
def prompter(monkeypatch):
    """A prompter at a terminal, with a saved app, a paired device and a client
    that only pretends to pair."""
    config = Config.defaults()
    config.session.spotify.client_id = "saved"
    client = SpotifyClient(config)
    client.pair_soloist = AsyncMock(return_value="user")
    client._soloist_backend = FakeBackend()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(prompter_module.Confirm, "ask", lambda *a, **k: True)
    return SpotifyPrompter(config, client)


async def test_the_prompter_asks_for_the_key_and_pairs_the_device(
    prompter, monkeypatch
):
    asked = []

    def ask(prompt, password=False, **_):
        asked.append(password)
        return "NEW-KEY"

    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(ask))

    await prompter._offer_spotify_backend()

    assert asked == [True]  # a secret: not echoed
    prompter.client.pair_soloist.assert_awaited_once_with("NEW-KEY")
    assert prompter.config.session.spotify.soloist_api_key == "NEW-KEY"


async def test_a_failed_pairing_does_not_fail_the_login(prompter):
    prompter.config.session.spotify.soloist_api_key = "KEY"
    prompter.client.pair_soloist = AsyncMock(side_effect=AuthenticationError("no"))

    await prompter._offer_spotify_backend()  # no raise

    prompter.client.pair_soloist.assert_awaited_once()


async def test_nothing_is_asked_without_a_terminal(prompter, monkeypatch):
    # From cron or a script a hidden question would hang for ever.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    await prompter._offer_spotify_backend()

    prompter.client.pair_soloist.assert_not_awaited()
