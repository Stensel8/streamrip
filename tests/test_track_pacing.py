"""The pacing of track starts: the paces (auto, fast, slow), the gaps, the 429
widening, the hour cap, what of it outlives a run, and how it is shown."""

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
    # As many separate episodes as there can be before the stop.
    for _ in range(client_module.RATE_LIMIT_BUDGET - 1):
        client._pause_requests(1.0)
        clock.now += 2.0  # let each pause end

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


def test_auto_turns_slow_at_the_first_429(paced, caplog):
    client, clock = paced
    client.pace("auto", 1)
    _elapse(client, clock)
    client._pause_requests(10.0)
    clock.now += 11
    assert _elapse(client, clock) > 0
    assert "it is refusing requests (HTTP 429)" in caplog.text


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


def test_the_load_counts_the_clock_hour_and_says_the_risk(paced, monkeypatch):
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
    assert Load("slow", True, 4, 4, 0).risk == "high"  # at the line
    assert Load("slow", True, 8, 8, 0).risk == "very high"  # twice it
    assert Load("slow", True, 0, 0, 1).risk == "medium"  # a first 429
    assert Load("slow", True, 0, 0, 2).risk == "high"
    assert Load("slow", True, 0, 0, 5).risk == "very high"  # half of what stops a rip


@pytest.mark.parametrize(("slow", "risk"), [(False, "medium"), (True, "low")])
def test_a_big_job_at_full_speed_is_a_risk_from_its_first_track(slow, risk):
    """Not only once the hour fills up: a job auto would pace."""
    assert Load("fast", slow, 1, 1, 0, big=True).risk == risk


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
    assert client.load().risk == "very high"


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


def test_the_load_line_says_the_pace_the_hour_and_the_risk():
    client = SimpleNamespace(
        source="tidal", load=lambda: Load("auto", True, 250, 300, 3)
    )
    out = Console(width=200, record=True, color_system=None)
    out.print(load_table([client]))
    text = out.export_text()
    assert text.startswith("Tidal ")
    assert text.rstrip().endswith(
        "250 of 175 tracks this hour · steady pace (auto) · 3 rate limits "
        "· ban risk high"
    )
    calm = SimpleNamespace(source="tidal", load=lambda: Load("fast", False, 1, 1, 0))
    out.print(load_table([calm]))
    assert (
        out.export_text()
        .rstrip()
        .endswith("1 of 175 tracks this hour · full speed · ban risk low")
    )


@pytest.mark.parametrize(
    ("load", "said"),
    [
        (
            Load("fast", False, 29, 46, 0, big=True),
            (
                "29 of 175 tracks this hour · full speed · ban risk medium "
                "· don't keep this up for long"
            ),
        ),
        (
            Load("fast", False, 180, 400, 0, hours_over=2),
            (
                "180 of 175 tracks this hour · full speed · ban risk very high "
                "· stop now, and rerun with --pace slow"
            ),
        ),
    ],
)
def test_the_load_line_says_what_to_do_at_full_speed(load, said):
    out = Console(width=200, record=True, color_system=None)
    out.print(load_table([SimpleNamespace(source="tidal", load=lambda: load)]))
    assert out.export_text().rstrip().endswith(said)


def test_a_track_waiting_for_its_turn_shows_when_it_comes(paced):
    """Between paced tracks nothing downloads; the line says why."""
    client, _ = paced
    client._reserve_track_start()
    client._reserve_track_start()  # its turn is a gap away
    assert client.load().next_in == pytest.approx(30.0)
    out = Console(width=200, record=True, color_system=None)
    out.print(load_table([client]))
    assert "steady pace · next track in 0:30 · ban risk low" in out.export_text()


def test_the_time_left_is_the_pace_s(paced, monkeypatch):
    """A gap per track while the hour has room, then TRACKS_PER_HOUR an hour;
    at full speed the downloads set the time, so it has none."""
    client, _ = paced
    monkeypatch.setattr(client_module, "TRACKS_PER_HOUR", 80)
    assert client.time_for(10) == pytest.approx(10 * 30)
    assert client.time_for(100) == pytest.approx(80 * 30 + 20 * 3600 / 80)
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
        f"1 track and no rate limits in the hour up to {up_to} (steady pace)."
        in caplog.text
    )


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


@pytest.mark.parametrize(
    "args",
    [
        ["--pace", "fast", "config", "path"],
        ["config", "path", "--pace", "fast"],
        ["config", "path", "--pace=FAST"],
    ],
)
def test_the_pace_flag_goes_before_or_after_the_command(tmp_path, args):
    """`streamrip search ... --pace fast` said "No such option": Click reads a
    group's options only before its command."""
    cfg = tmp_path / "config.toml"
    set_user_defaults(cfg)
    obj = {}
    result = CliRunner().invoke(rip, ["--config-path", str(cfg), *args], obj=obj)
    assert result.exit_code == 0, result.output
    assert obj["config"].session.downloads.pacing == "fast"


def test_a_command_keeps_its_own_short_options(tmp_path):
    """Short options are not moved: -f is the group's --folder before a command,
    and search's own --first after it."""
    cfg = tmp_path / "config.toml"
    set_user_defaults(cfg)
    result = CliRunner().invoke(
        rip, ["--config-path", str(cfg), "config", "path", "-f", "x"]
    )
    assert result.exit_code == 2
    assert "No such option '-f'" in result.output


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
