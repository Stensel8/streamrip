"""The pacing of track starts: the paces (auto, fast, medium, slow), the
gaps, the 429 widening, the hour cap, what of it outlives a run, and how it
is shown."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

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
from streamrip.media import Album, Artist, Playlist, Track
from streamrip.rip.cli import rip
from streamrip.rip.main import ALBUM_TRACKS, load_table, planned_tracks

# A draw far above TRACK_GAP_MAX, so the cap is what every gap comes out at.
_HUGE = 10_000.0

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


# --- the gaps -----------------------------------------------------------------


async def test_track_starts_are_a_human_sized_gap_apart(paced, monkeypatch):
    client, _ = paced
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await client.before_track()  # the first track starts right away
    await client.before_track()
    assert slept == [30.0]
    assert client.load().next_in == pytest.approx(30.0)  # what the load line shows


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
    """Counting the starts reserved ahead too: rip_tracks resolves a few
    tracks side by side, and each reserves its start before the last one's
    has come."""
    client, clock = paced
    monkeypatch.setattr(client_module, "TRACK_GAP_MAX", 0)  # all starts at once
    monkeypatch.setitem(client_module.PACE_TRACKS_PER_HOUR, "slow", 3)
    now, hour = clock.now, client_module.RATE_LIMIT_WINDOW

    starts = [now + client._reserve_track_start() for _ in range(7)]
    assert starts == [now] * 3 + [now + hour] * 3 + [now + 2 * hour]


def test_track_pacing_is_shared_with_the_pause_owner(monkeypatch, paced):
    main, clock = paced
    bootstrap = QobuzClient(Config.defaults())
    monkeypatch.setattr(bootstrap, "_pause_owner", lambda: main)
    _elapse(main, clock)

    assert bootstrap._reserve_track_start() == pytest.approx(30.0)


# --- what outlives a run ------------------------------------------------------


def test_pacing_outlives_a_run(runs, tmp_path):
    """A ripper that starts streamrip once per job still shares one pacing."""
    run, clock = runs
    (tmp_path / "pacing.json").write_text("}{ not json")
    first = run()
    assert _elapse(first, clock) == 0.0  # unreadable: a fresh start
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


def test_tracks_of_earlier_runs_count_towards_autos_budget(runs, monkeypatch):
    """A ripper starting streamrip for one small job after the other still
    turns slow: the tracks of the last hours are in the pacing state."""
    monkeypatch.setattr(client_module, "FAST_BUDGET", 3)
    run, clock = runs
    first = run("auto", 2)
    _elapse(first, clock)
    _elapse(first, clock)
    assert not first.load().slow
    assert run("auto", 2).load().slow  # 2 to come and 2 before: past 3


# --- the paces ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("planned", "gaps"),
    [
        (2, [0.0, 0.0, 0.0, 30.0]),  # until the budget is spent
        (4, [0.0, 30.0, 30.0, 30.0]),  # slow, never medium, from the first track
    ],
    ids=["small job", "big job"],
)
def test_auto_rips_only_a_small_job_at_full_speed(
    paced, monkeypatch, caplog, planned, gaps
):
    """15 albums are never ripped at full speed: the pace is chosen before the
    first track, for the job as a whole."""
    monkeypatch.setattr(client_module, "FAST_BUDGET", 3)
    client, clock = paced
    client.pace("auto", planned)
    assert [_elapse(client, clock) for _ in range(4)] == pytest.approx(gaps)
    assert client.load().tier == "slow"
    # A big job is told how to rip it without the waits.
    assert ("Add --pace fast" in caplog.text) is (planned > 3)


@pytest.mark.parametrize(("pace", "tier"), [("auto", "slow"), ("medium", "medium")])
def test_a_429_turns_only_auto_slow(paced, pace, tier):
    """medium, fast and slow, asked for by name, are not overridden."""
    client, clock = paced
    client.pace(pace, 1)
    _elapse(client, clock)
    client._pause_requests(10.0)
    clock.now += 11
    assert _elapse(client, clock) > 0
    assert client.load().tier == tier


def test_full_speed_never_waits_and_warns_once_an_hour_louder_from_the_second(
    paced, caplog, monkeypatch
):
    """Hour after hour over the line is how the account was suspended."""
    monkeypatch.setattr(client_module, "HOURLY_DANGER", 3)
    monkeypatch.setattr(client_module, "FAST_BUDGET", 1)
    client, clock = paced
    client.pace("fast", 10)  # a big job, at full speed all the same
    assert [_elapse(client, clock) for _ in range(5)] == [0.0] * 5
    clock.now += 3600
    for _ in range(3):
        _elapse(client, clock)

    over = [r.levelname for r in caplog.records if "at full speed" in r.message]
    assert over == ["WARNING", "ERROR"]
    assert client.load().risk == "very high"


def test_each_rate_limit_is_said_louder_up_to_the_stop(paced, caplog):
    """A first one is a wait, from half of the budget on they are red, and
    the stop says how long to leave the account alone."""
    client, clock = paced
    for _ in range(client_module.RATE_LIMIT_BUDGET - 1):
        client._pause_requests(10.0)
        clock.now += 11  # the pause ends: the next one is a separate episode
    said = [r for r in caplog.records if "rate limit" in r.message]
    assert "first rate limit (HTTP 429) detected. Let's wait a bit" in said[0].message
    assert "rate limit 2 of 10 in the last hour (at 10 streamrip stops)" in (
        said[1].message
    )
    assert [r.levelname for r in said].index("ERROR") == 4
    assert "not good, rate limit 5 of 10" in said[4].message
    with pytest.raises(AccountHalt, match="Best to wait about 3 hours, until around"):
        client._pause_requests(10.0)


# --- the load -----------------------------------------------------------------


def test_the_load_counts_the_clock_hour(paced, monkeypatch):
    """The service counts per clock hour, so that is the hour the load shows."""
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
        (Load("slow", "slow", 0, 0, 2), "high"),
        (Load("slow", "slow", 0, 0, 5), "very high"),  # half of what stops a rip
        # A job auto would pace is a risk at full speed from its first track,
        # not only once the hour fills up.
        (Load("fast", "fast", 1, 1, 0, big=True), "medium"),
        (Load("fast", "slow", 1, 1, 0, big=True), "low"),
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
                "250 of 175 tracks this hour · slow pace (auto) · 3 rate limits "
                "· ban risk high"
            ),
        ),
        (
            Load("fast", "fast", 1, 1, 0),
            "1 of 175 tracks this hour · full speed · ban risk low",
        ),
        (
            Load("slow", "slow", 1, 1, 0, next_in=30.0),
            # Between paced tracks nothing downloads; the line says why.
            "slow pace · next track in 0:30 · ban risk low",
        ),
        (
            Load("fast", "fast", 29, 46, 0, big=True),
            (
                "29 of 175 tracks this hour · full speed · ban risk medium "
                "· don't keep this up for long"
            ),
        ),
        (
            Load("fast", "fast", 180, 400, 0, hours_over=2),
            (
                "180 of 175 tracks this hour · full speed · ban risk very high "
                "· stop now, and rerun with --pace slow"
            ),
        ),
    ],
)
def test_the_load_line(load, said):
    out = Console(width=200, record=True, color_system=None)
    out.print(load_table([SimpleNamespace(source="tidal", load=lambda: load)]))
    text = out.export_text()
    assert text.startswith("Tidal ")
    assert text.rstrip().endswith(said)


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


@pytest.mark.parametrize("tier", ["slow", "medium"])
def test_the_time_left_is_the_pace_s(paced, monkeypatch, tier):
    """A gap per track while the hour has room, then the tier's own hourly
    cap (the real one, not conftest's stand-in). At full speed the downloads
    set the time, so it has none."""
    client, _ = paced
    cap, mean = _REAL_PACE_TRACKS_PER_HOUR[tier], client_module.PACE_GAP_MEAN[tier]
    monkeypatch.setitem(client_module.PACE_TRACKS_PER_HOUR, tier, cap)
    client.pace(tier, 1)
    assert client.time_for(10) == pytest.approx(10 * mean)
    assert client.time_for(cap + 20) == pytest.approx(
        cap * mean + 20 * max(mean, 3600 / cap)
    )
    client.pace("fast", 1)
    assert client.time_for(10) is None


def test_the_overall_bar_counts_down_at_the_pace():
    """Rich's estimate goes by the tracks finished in the last half minute,
    and paced ones finish further apart: it showed -:--:-- or a wild guess."""
    bar = progress._p.overall_progress
    task_id = bar.add_task("Done", total=10, completed=4)
    task = next(t for t in bar.tasks if t.id == task_id)
    column = progress._TimeLeft(progress._p)
    try:
        progress.show_load(lambda: "", time_for=lambda tracks: tracks * 30.0)
        assert column.render(task).plain == "0:03:00"
        progress.show_load(None)
        assert column.render(task).plain == "-:--:--"  # Rich's own
    finally:
        progress.show_load(None)
        bar.remove_task(task_id)


def test_the_load_line_is_redrawn_under_the_progress_bars():
    """Not only once the run is over: under the bars, as they go."""
    try:
        progress.show_load(lambda: "the load", enabled=True)
        drawn = [
            part for part in progress._p._group().renderables if hasattr(part, "render")
        ]
        assert [part.__rich__() for part in drawn] == ["the load"]
    finally:
        progress.show_load(None)
    assert not any(hasattr(p, "render") for p in progress._p._group().renderables)


def test_each_clock_hour_is_logged_once_it_is_over(paced, caplog):
    """A long run without progress bars (a ripper's log) still shows how hard
    it worked, in the unit the service counts in."""
    client, clock = paced
    _elapse(client, clock)  # 00:16 UTC on the fake clock
    clock.now = 3600 + 60
    _elapse(client, clock)
    # In local time, as the log's own timestamps.
    up_to = datetime.fromtimestamp(3600).strftime("%H:%M")
    assert (
        f"1 track and no rate limits in the hour up to {up_to} (slow pace)."
        in caplog.text
    )


# --- the config and the command line -------------------------------------------


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
        (["config", "path", "--pace=FAST"], "fast"),
    ],
)
def test_the_pace_flag_goes_before_or_after_the_command(config_file, args, pacing):
    obj = {}
    result = CliRunner().invoke(
        rip, ["--config-path", str(config_file), *args], obj=obj
    )
    assert result.exit_code == 0, result.output
    assert obj["config"].session.downloads.pacing == pacing


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--pace", "turbo", "config", "path"], "turbo"),
        # Short options are not moved: -f is the group's --folder before a
        # command, and search's own --first after it.
        (["config", "path", "-f", "x"], "No such option '-f'"),
    ],
)
def test_the_command_line_refuses(config_file, args, error):
    result = CliRunner().invoke(rip, ["--config-path", str(config_file), *args])
    assert result.exit_code == 2
    assert error in result.output


def test_every_client_finds_its_pacing_state(config_file):
    """Every source builds its client with a real Config, so every one of
    them finds the file its pacing outlives a run in."""
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
    assert (qobuz._cap("slow"), tidal._cap("slow")) == (40, 80)
    assert qobuz._gap_mean("slow") == pytest.approx(2 * tidal._gap_mean("slow"))
    qobuz.pace("auto", 51)  # past its own full_speed_tracks
    assert (qobuz.load().tier, qobuz.load().cap) == ("slow", 40)
    assert qobuz.load().hourly_line == 300
    assert tidal.load().hourly_line == client_module.HOURLY_DANGER
