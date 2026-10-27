"""The pacing of track starts: the paces (auto, fast, slow), the gaps, the 429
widening, the hour cap, and what of it outlives a run."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from streamrip.client import client as client_module
from streamrip.client.deezer import DeezerClient
from streamrip.client.qobuz import QobuzClient
from streamrip.client.soundcloud import SoundcloudClient
from streamrip.client.spotify import SpotifyClient
from streamrip.client.tidal import TidalClient
from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import AccountHalt
from streamrip.media import Album, Artist, Playlist, Track
from streamrip.rip.cli import rip
from streamrip.rip.main import ALBUM_TRACKS, planned_tracks

# A draw far above TRACK_GAP_MAX, so the cap is what every gap comes out at.
_HUGE = 10_000.0


class _Clock:
    """client_module's time on a clock a test advances by hand."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now


@pytest.fixture
def paced(monkeypatch):
    """A client at the slow pace whose gaps between track starts all come
    out at 30s, on a fake clock."""
    clock = _Clock()
    monkeypatch.setattr(client_module, "time", clock)
    monkeypatch.setattr(client_module, "TRACK_GAP_MAX", 30)
    monkeypatch.setattr(
        client_module, "random", SimpleNamespace(lognormvariate=lambda mu, sigma: _HUGE)
    )
    client = QobuzClient(Config.defaults())
    client.pace("slow", 0)
    return client, clock


@pytest.fixture
def runs(paced, tmp_path, monkeypatch):
    """A new client for every call, all of one account and one state file:
    one run after another, as a ripper starts them."""
    _, clock = paced
    state = tmp_path / "pacing.json"

    def run(pace="slow", planned=0):
        client = QobuzClient(Config.defaults())
        monkeypatch.setattr(client, "_pacing_state_path", lambda: state)
        client.pace(pace, planned)
        return client

    return run, clock


def _elapse(client, clock) -> float:
    """Reserve a track start and let its wait pass."""
    wait = client._reserve_track_start()
    clock.now += wait
    return wait


def test_the_gap_between_track_starts_is_human_sized(paced):
    client, clock = paced
    assert _elapse(client, clock) == 0.0  # the first track starts at once
    assert _elapse(client, clock) == pytest.approx(30.0)


async def test_before_track_sleeps_the_reserved_gap(paced, monkeypatch):
    client, _ = paced
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await client.before_track()  # the first track starts right away
    await client.before_track()
    assert slept == [30.0]


def test_a_429_episode_widens_the_gaps_and_clean_tracks_ease_them_back(paced):
    client, clock = paced
    _elapse(client, clock)
    client._pause_requests(10.0)  # a new 429 episode
    client._pause_requests(10.0)  # the pause is still on: the same episode
    assert client._track_gap_scale == pytest.approx(client_module.TRACK_GAP_WIDENING)

    assert _elapse(client, clock) == pytest.approx(
        30.0 * client_module.TRACK_GAP_WIDENING
    )
    # And each track that starts without a 429 in between eases it back.
    eased = client_module.TRACK_GAP_WIDENING * client_module.TRACK_GAP_EASING
    assert _elapse(client, clock) == pytest.approx(30.0 * eased)


def test_the_widening_has_a_ceiling(paced):
    client, clock = paced
    _elapse(client, clock)
    for _ in range(10):
        client._pause_requests(1.0)
        clock.now += 2.0  # let each pause end: ten separate episodes

    assert client._track_gap_scale == client_module.TRACK_GAP_WIDENING_MAX
    assert _elapse(client, clock) == pytest.approx(
        30.0 * client_module.TRACK_GAP_WIDENING_MAX
    )


def test_the_hour_cap_holds_back_the_next_track(monkeypatch, paced, caplog):
    """Counting the starts reserved ahead too: rip_tracks resolves a few
    tracks side by side, and each reserves its start before the last one's
    has come."""
    client, clock = paced
    monkeypatch.setattr(client_module, "TRACK_GAP_MAX", 0)  # all starts at once
    monkeypatch.setattr(client_module, "TRACKS_PER_HOUR", 3)
    now, hour = clock.now, client_module.RATE_LIMIT_WINDOW

    starts = [now + client._reserve_track_start() for _ in range(7)]
    assert starts == [now] * 3 + [now + hour] * 3 + [now + 2 * hour]
    assert "3 tracks started in the last hour" in caplog.text


def test_track_pacing_is_shared_with_the_pause_owner(monkeypatch, paced):
    main, clock = paced
    bootstrap = QobuzClient(Config.defaults())
    monkeypatch.setattr(bootstrap, "_pause_owner", lambda: main)
    _elapse(main, clock)

    # A client sharing the account's pause shares its track pacing.
    assert bootstrap._reserve_track_start() == pytest.approx(30.0)


def test_pacing_outlives_a_run(runs):
    """A ripper that starts streamrip once per job still shares one pacing."""
    run, clock = runs
    first = run()
    _elapse(first, clock)
    _elapse(first, clock)
    assert _elapse(run(), clock) == pytest.approx(30.0)


def test_an_hour_of_quiet_is_a_fresh_start_widened_gaps_and_all(runs):
    """The wait a halt asks for is the cooldown: the run after it starts at
    the usual gaps, not at the widest."""
    run, clock = runs
    first = run()
    _elapse(first, clock)
    for _ in range(3):
        first._pause_requests(1.0)
        clock.now += 2.0  # let each pause end: three separate episodes
    assert first._track_gap_scale > 1.0

    clock.now += client_module.RATE_LIMIT_WINDOW
    later = run()
    assert _elapse(later, clock) == 0.0
    assert _elapse(later, clock) == pytest.approx(30.0)


async def test_a_run_started_while_the_budget_is_spent_stops_at_its_first_request(
    runs, monkeypatch
):
    """Artist and album requests go out before any track: a halt the last run
    left has to stop this one at its very first request."""
    run, clock = runs
    monkeypatch.setattr(client_module, "RATE_LIMIT_BUDGET", 2)
    first = run()
    first._pause_requests(1.0)
    clock.now += 2.0  # the pause ends: a separate episode
    with pytest.raises(AccountHalt):
        first._pause_requests(1.0)

    successor = run()
    successor.session = SimpleNamespace(
        get=lambda *a, **k: pytest.fail("a halted run sends no request")
    )
    with pytest.raises(AccountHalt):
        await successor._get_with_retries("https://example.com", lambda resp: None)


def test_an_unreadable_state_file_is_a_fresh_start(runs, tmp_path):
    run, _ = runs
    (tmp_path / "pacing.json").write_text("}{ not json")
    assert run()._reserve_track_start() == 0.0


def test_auto_rips_a_small_job_at_full_speed(paced, caplog):
    client, clock = paced
    client.pace("auto", 2)
    assert [_elapse(client, clock) for _ in range(2)] == [0.0, 0.0]
    assert "2 tracks to download, a small job, so at full speed" in caplog.text


def test_auto_goes_slow_from_the_first_track_for_a_big_job(paced, caplog, monkeypatch):
    """15 albums are never ripped at full speed: the pace is chosen before the
    first track, for the job as a whole."""
    monkeypatch.setattr(client_module, "FAST_BUDGET", 3)
    client, clock = paced
    client.pace("auto", 4)
    assert _elapse(client, clock) == 0.0
    assert _elapse(client, clock) == pytest.approx(30.0)
    assert "4 tracks to download, so at a steady pace" in caplog.text
    assert "Add --pace fast to rip without waiting between tracks." in caplog.text


def test_auto_turns_slow_once_its_budget_is_spent(paced, caplog, monkeypatch):
    monkeypatch.setattr(client_module, "FAST_BUDGET", 3)
    client, clock = paced
    client.pace("auto", 1)  # said to be small, and turns out bigger
    assert [_elapse(client, clock) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert _elapse(client, clock) == pytest.approx(30.0)
    assert (
        "3 tracks in the last 6 hours, so from here on at a steady pace" in caplog.text
    )


def test_fast_never_waits_and_warns_once_an_hour_at_the_danger(
    paced, caplog, monkeypatch
):
    monkeypatch.setattr(client_module, "HOURLY_DANGER", 3)
    monkeypatch.setattr(client_module, "FAST_BUDGET", 1)
    client, clock = paced
    client.pace("fast", 10)
    assert [_elapse(client, clock) for _ in range(5)] == [0.0] * 5
    assert "full speed for 10 tracks, as asked (--pace fast)" in caplog.text
    assert caplog.text.count("tracks this hour at full speed") == 1


def test_a_second_hour_over_the_line_is_said_in_red(paced, caplog, monkeypatch):
    """Hour after hour over the limit is how the account was suspended."""
    monkeypatch.setattr(client_module, "HOURLY_DANGER", 3)
    client, clock = paced
    client.pace("fast", 0)
    for _ in range(3):
        _elapse(client, clock)
    over = [r for r in caplog.records if "at full speed" in r.message]
    assert [r.levelname for r in over] == ["WARNING"]
    clock.now += 3600
    for _ in range(3):
        _elapse(client, clock)
    over = [r for r in caplog.records if "at full speed" in r.message]
    assert [r.levelname for r in over] == ["WARNING", "ERROR"]
    assert "in 2 of the last 6 hours" in over[-1].message


def test_the_tracks_to_come_are_counted_per_account():
    """An album's and a playlist's tracks by the account each comes from, an
    artist's albums as ALBUM_TRACKS each, and nothing that is no media."""
    a, b = object(), object()
    album = object.__new__(Album)
    album.tracks = [SimpleNamespace(client=a)] * 12
    playlist = object.__new__(Playlist)
    playlist.tracks = [SimpleNamespace(client=a)] * 3 + [SimpleNamespace(client=b)] * 2
    artist = object.__new__(Artist)
    artist.client, artist.albums = b, [None] * 15
    track = object.__new__(Track)
    track.client = a
    planned = planned_tracks([album, playlist, artist, track, MagicMock()])
    assert planned == {a: 16, b: 2 + 15 * ALBUM_TRACKS}


def test_the_pace_is_optional_in_the_config_and_checked(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    assert Config(path).session.downloads.pacing == "auto"
    path.write_text(path.read_text().replace('# pacing = "auto"', 'pacing = "slow"'))
    assert Config(path).session.downloads.pacing == "slow"
    path.write_text(path.read_text().replace('pacing = "slow"', 'pacing = "turbo"'))
    with pytest.raises(ValueError, match="turbo"):
        Config(path)


@pytest.mark.parametrize(("pace", "code"), [("SLOW", 0), ("turbo", 2)])
def test_the_pace_flag_takes_only_a_pace(tmp_path, pace, code):
    cfg = tmp_path / "config.toml"
    set_user_defaults(cfg)
    result = CliRunner().invoke(
        rip, ["--config-path", str(cfg), "--pace", pace, "config", "path"]
    )
    assert result.exit_code == code, result.output


# The real method, captured at import: conftest.py's autouse fixture silences
# it for every test, this file's own included.
_real_pacing_state_path = client_module.RequestClient._pacing_state_path


def test_every_client_finds_its_pacing_state(tmp_path):
    """Every source builds its client with a real Config, so every one of
    them finds the file its pacing outlives a run in."""
    config = tmp_path / "config.toml"
    set_user_defaults(config)
    loaded = Config(config)
    for client in (
        QobuzClient(loaded),
        TidalClient(loaded),
        DeezerClient(loaded),
        SoundcloudClient(loaded),
        SpotifyClient(loaded),
    ):
        assert _real_pacing_state_path(client) == (
            tmp_path / f"pacing-{client.source}.json"
        )
