"""The pacing of track starts: the paces (auto, fast, medium, slow), the gaps,
the 429 widening, the hour cap, what of it outlives a run, and how it is shown."""

import asyncio
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

from streamrip import progress
from streamrip.client import client as client_module
from streamrip.client.client import Load
from streamrip.client.deezer import DeezerClient
from streamrip.client.qobuz import QobuzClient
from streamrip.client.soundcloud import SoundcloudClient
from streamrip.client.spotify import SpotifyClient
from streamrip.client.tidal import TidalClient
from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import AccountHalt
from streamrip.rip.cli import rip
from streamrip.rip.main import load_table

# The real caps and state file, captured at import: conftest.py's autouse
# _no_track_pacing patches them away for every test, this file's own included.
_REAL_PACE_TRACKS_PER_HOUR = dict(client_module.PACE_TRACKS_PER_HOUR)
_real_pacing_state_path = client_module.RequestClient._pacing_state_path


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
    """A client at the slow pace whose gaps between track starts all come out
    at 30s, on a fake clock."""
    clock = _Clock()
    monkeypatch.setattr(client_module, "time", clock)
    monkeypatch.setattr(client_module, "TRACK_GAP_MAX", 30)
    # A draw far above the cap, so the cap is what every gap comes out at.
    huge = SimpleNamespace(lognormvariate=lambda mu, sigma: 10_000.0)
    monkeypatch.setattr(client_module, "random", huge)
    client = QobuzClient(Config.defaults())
    client.pace("slow", 0)
    return client, clock


@pytest.fixture
def runs(paced, tmp_path, monkeypatch):
    """A new client for every call, all of one account and one state file: one
    run after another, as a ripper starts them."""
    _, clock = paced
    state = tmp_path / "pacing.json"

    def run(pace="slow", planned=0):
        client = QobuzClient(Config.defaults())
        monkeypatch.setattr(client, "_pacing_state_path", lambda: state)
        client.pace(pace, planned)
        return client

    return run, clock


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    return path


def _elapse(client, clock) -> float:
    """Reserve a track start and let its wait pass."""
    wait = client._reserve_track_start()
    clock.now += wait
    return wait


async def test_track_starts_are_a_human_sized_gap_apart(paced, monkeypatch):
    client, _ = paced
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await client.before_track()  # the first track starts right away
    await client.before_track()
    assert slept == [30.0]
    assert client.load().next_in == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("episodes", "scale"),
    [
        (1, client_module.TRACK_GAP_WIDENING),
        (client_module.RATE_LIMIT_BUDGET - 1, client_module.TRACK_GAP_WIDENING_MAX),
    ],
    ids=["one", "up to the ceiling"],
)
def test_429s_widen_the_gaps_and_clean_tracks_ease_them_back(paced, episodes, scale):
    client, clock = paced
    _elapse(client, clock)
    for _ in range(episodes):
        client._pause_requests(1.0)  # a new 429 episode
        client._pause_requests(1.0)  # the pause is still on: the same episode
        clock.now += 2.0  # the pause ends: the next one is a separate episode

    assert _elapse(client, clock) == pytest.approx(30.0 * scale)
    eased = scale * client_module.TRACK_GAP_EASING
    assert _elapse(client, clock) == pytest.approx(30.0 * eased)


def test_the_hour_cap_holds_back_the_next_track(monkeypatch, paced):
    # rip_tracks resolves a few tracks side by side: each reserves its start early.
    client, clock = paced
    monkeypatch.setattr(client_module, "TRACK_GAP_MAX", 0)  # all starts at once
    monkeypatch.setitem(client_module.PACE_TRACKS_PER_HOUR, "slow", 3)
    now, hour = clock.now, client_module.RATE_LIMIT_WINDOW

    starts = [now + client._reserve_track_start() for _ in range(7)]
    assert starts == [now] * 3 + [now + hour] * 3 + [now + 2 * hour]


def test_pacing_outlives_a_run(runs, tmp_path):
    run, clock = runs
    (tmp_path / "pacing.json").write_text("}{ not json")
    first = run()
    assert _elapse(first, clock) == 0.0  # unreadable: a fresh start
    _elapse(first, clock)
    assert _elapse(run(), clock) == pytest.approx(30.0)


def test_an_hour_of_quiet_is_a_fresh_start_widened_gaps_and_all(runs):
    run, clock = runs
    first = run()
    _elapse(first, clock)
    for _ in range(3):
        first._pause_requests(1.0)
        clock.now += 2.0  # three separate episodes
    assert first._track_gap_scale > 1.0

    clock.now += client_module.RATE_LIMIT_WINDOW
    later = run()
    assert _elapse(later, clock) == 0.0
    assert _elapse(later, clock) == pytest.approx(30.0)


def test_a_clock_set_back_since_the_last_run_is_no_wait(runs):
    # The last run went while the clock was two hours ahead (a dual boot).
    run, clock = runs
    clock.now += 7200
    _elapse(run(), clock)
    clock.now -= 7200
    assert _elapse(run(), clock) == pytest.approx(30.0)  # not two hours and more


async def test_a_run_started_while_the_budget_is_spent_stops_at_its_first_request(
    runs, monkeypatch
):
    run, clock = runs
    monkeypatch.setattr(client_module, "RATE_LIMIT_BUDGET", 2)
    first = run()
    first._pause_requests(1.0)
    clock.now += 2.0
    with pytest.raises(AccountHalt):
        first._pause_requests(1.0)

    successor = run()
    successor.session = SimpleNamespace(
        get=lambda *a, **k: pytest.fail("a halted run sends no request")
    )
    with pytest.raises(AccountHalt):
        await successor._get_with_retries("https://example.com", lambda resp: None)


def test_tracks_of_earlier_runs_count_towards_autos_budget(runs, monkeypatch):
    monkeypatch.setattr(client_module, "FAST_BUDGET", 3)
    run, clock = runs
    first = run("auto", 2)
    _elapse(first, clock)
    _elapse(first, clock)
    assert not first.load().slow
    assert run("auto", 2).load().slow  # 2 to come and 2 before: past 3


@pytest.mark.parametrize(
    ("planned", "gaps"),
    [
        (2, [0.0, 0.0, 0.0, 30.0]),  # until the budget is spent
        (4, [0.0, 30.0, 30.0, 30.0]),  # slow from the first track: the job is big
    ],
    ids=["small job", "big job"],
)
def test_auto_rips_only_a_small_job_at_full_speed(paced, monkeypatch, planned, gaps):
    monkeypatch.setattr(client_module, "FAST_BUDGET", 3)
    client, clock = paced
    client.pace("auto", planned)
    assert [_elapse(client, clock) for _ in range(4)] == pytest.approx(gaps)
    assert client.load().tier == "slow"


@pytest.mark.parametrize(("pace", "tier"), [("auto", "slow"), ("medium", "medium")])
def test_a_429_turns_only_auto_slow(paced, pace, tier):
    client, clock = paced
    client.pace(pace, 1)
    _elapse(client, clock)
    client._pause_requests(10.0)
    clock.now += 11
    assert _elapse(client, clock) > 0
    assert client.load().tier == tier


def test_full_speed_never_waits_but_the_load_says_how_risky_it_is(paced, monkeypatch):
    monkeypatch.setattr(client_module, "HOURLY_DANGER", 3)
    monkeypatch.setattr(client_module, "FAST_BUDGET", 1)
    client, clock = paced
    client.pace("fast", 10)  # a big job, at full speed all the same
    assert [_elapse(client, clock) for _ in range(5)] == [0.0] * 5
    clock.now += 3600
    for _ in range(3):
        _elapse(client, clock)
    assert client.load().risk == "very high"


def test_the_load_counts_the_clock_hour(paced, monkeypatch):
    monkeypatch.setattr(client_module, "HOURLY_DANGER", 4)
    client, clock = paced
    client.pace("fast", 0)
    _elapse(client, clock)
    _elapse(client, clock)
    load = client.load()
    assert (load.this_hour, load.recent, load.limited, load.slow) == (2, 2, 0, False)
    assert load.risk == "medium"  # half of the line
    clock.now = 2 * 3600  # the next clock hour but one
    assert (client.load().this_hour, client.load().risk) == (0, "low")


@pytest.mark.parametrize(
    ("load", "risk"),
    [
        (Load("slow", "slow", 175, 175, 0), "high"),  # at the line
        (Load("slow", "slow", 350, 350, 0), "very high"),  # twice it
        (Load("slow", "slow", 0, 0, 1), "medium"),  # a first 429
        (Load("slow", "slow", 0, 0, 5), "very high"),  # half of what stops a rip
        (Load("fast", "fast", 1, 1, 0, big=True), "medium"),  # auto would pace it
    ],
)
def test_the_risk_of_a_load(load, risk):
    assert load.risk == risk


@pytest.mark.parametrize(
    ("load", "said"),
    [
        (
            Load("auto", "slow", 250, 300, 3),
            (
                "250 of 175 tracks this hour · slow pace (auto) · 3 rate limits · ban risk high"
            ),
        ),
        (
            Load("slow", "slow", 1, 1, 0, next_in=30.0),
            "slow pace · next track in 0:30 · ban risk low",
        ),
    ],
)
def test_the_load_line(load, said):
    out = Console(width=200, record=True, color_system=None)
    out.print(load_table([SimpleNamespace(source="tidal", load=lambda: load)]))
    text = out.export_text()
    assert text.startswith("Tidal ")
    assert text.rstrip().endswith(said)


def test_the_overall_bar_counts_down_at_the_pace():
    # Rich's own estimate goes by the tracks finished in the last half minute,
    # and paced ones finish further apart: it showed -:--:-- or a wild guess.
    bar = progress._p.overall_progress
    task_id = bar.add_task("Done", total=10, completed=4)
    task = next(t for t in bar.tasks if t.id == task_id)
    column = progress._TimeLeft(progress._p)
    try:
        progress.show_load(lambda: "", time_for=lambda tracks: tracks * 30.0)
        assert column.render(task).plain == "0:03:00"
        progress.show_load(None)
        assert column.render(task).plain == "-:--:--"
    finally:
        progress.show_load(None)
        bar.remove_task(task_id)


def test_the_pace_is_optional_in_the_config_and_checked(config_file):
    assert Config(config_file).session.downloads.pacing == "auto"
    text = config_file.read_text()
    config_file.write_text(text.replace('# pacing = "auto"', 'pacing = "slow"'))
    assert Config(config_file).session.downloads.pacing == "slow"
    config_file.write_text(text.replace('# pacing = "auto"', 'pacing = "turbo"'))
    with pytest.raises(ValueError, match="turbo"):
        Config(config_file)


@pytest.mark.parametrize(
    ("args", "pacing"),
    [
        (["--pace", "SLOW", "config", "path"], "slow"),
        # `streamrip search ... --pace fast` said "No such option": Click reads
        # a group's options only before its command.
        (["config", "path", "--pace", "fast"], "fast"),
    ],
)
def test_the_pace_flag_goes_before_or_after_the_command(config_file, args, pacing):
    obj = {}
    result = CliRunner().invoke(
        rip, ["--config-path", str(config_file), *args], obj=obj
    )
    assert result.exit_code == 0, result.output
    assert obj["config"].session.downloads.pacing == pacing


def test_an_unknown_pace_is_refused(config_file):
    args = ["--config-path", str(config_file), "--pace", "turbo", "config", "path"]
    result = CliRunner().invoke(rip, args)
    assert result.exit_code == 2
    assert "turbo" in result.output


def test_every_client_finds_its_pacing_state(config_file):
    loaded = Config(config_file)
    for client in (
        QobuzClient(loaded),
        TidalClient(loaded),
        DeezerClient(loaded),
        SoundcloudClient(loaded),
        SpotifyClient(loaded),
    ):
        assert _real_pacing_state_path(client) == (
            config_file.parent / f"pacing-{client.source}.json"
        )


def test_a_service_can_have_limits_of_its_own(monkeypatch):
    monkeypatch.setattr(
        client_module, "PACE_TRACKS_PER_HOUR", _REAL_PACE_TRACKS_PER_HOUR
    )
    config = Config.defaults()
    q = config.session.qobuz
    q.requests_per_minute, q.slow_tracks_per_hour = 120, 40
    q.max_tracks_per_hour, q.full_speed_tracks = 300, 50
    qobuz, tidal = QobuzClient(config), TidalClient(config)

    assert qobuz.rate_limiter.time_period == pytest.approx(60 / 120)
    assert tidal.rate_limiter.time_period == pytest.approx(60 / 35)  # [downloads]
    qobuz.pace("auto", 51)  # past its own full_speed_tracks
    assert (qobuz.load().tier, qobuz.load().cap) == ("slow", 40)
    assert qobuz.load().hourly_line == 300
    assert tidal.load().hourly_line == client_module.HOURLY_DANGER

    # 0 held every track back for good, or failed: one at least.
    q.slow_tracks_per_hour = q.max_tracks_per_hour = q.full_speed_tracks = 0
    assert (qobuz._cap("slow"), qobuz._line(), qobuz._fast_budget()) == (1, 1, 1)
    qobuz._reserve_track_start()
