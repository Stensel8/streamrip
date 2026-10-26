"""The human pacing of track starts: the gaps, the 429 widening, the hour cap."""

import asyncio
from types import SimpleNamespace

import pytest

from streamrip.client import client as client_module
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config

# A draw far above TRACK_GAP_MAX, so the cap is what every gap comes out at.
_HUGE = 10_000.0


class _Clock:
    """client_module's time on a clock a test advances by hand."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def paced(monkeypatch):
    """A client whose gaps between track starts all come out at 30s, on a
    fake clock."""
    clock = _Clock()
    monkeypatch.setattr(client_module, "time", clock)
    monkeypatch.setattr(client_module, "TRACK_GAP_MAX", 30)
    monkeypatch.setattr(
        client_module, "random", SimpleNamespace(lognormvariate=lambda mu, sigma: _HUGE)
    )
    return QobuzClient(Config.defaults()), clock


def _elapse(client, clock) -> float:
    """Reserve a track start and let its wait pass."""
    wait = client._reserve_track_start()
    clock.now += wait
    return wait


def test_the_first_track_of_a_session_starts_right_away(paced):
    client, _ = paced
    assert client._reserve_track_start() == 0.0


def test_the_gap_between_track_starts_is_human_sized(paced):
    client, clock = paced
    assert _elapse(client, clock) == 0.0
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
    client, clock = paced
    monkeypatch.setattr(client_module, "TRACK_GAP_MAX", 0)  # all starts at once
    monkeypatch.setattr(client_module, "TRACKS_PER_HOUR", 3)
    for _ in range(3):
        assert _elapse(client, clock) == 0.0

    assert _elapse(client, clock) == pytest.approx(client_module.RATE_LIMIT_WINDOW)
    assert "in the last hour" in caplog.text

    # Once the oldest start is an hour old, tracks flow again right away.
    clock.now += client_module.RATE_LIMIT_WINDOW
    assert _elapse(client, clock) == 0.0


def test_track_pacing_is_shared_with_the_pause_owner(monkeypatch, paced):
    main, clock = paced
    bootstrap = QobuzClient(Config.defaults())
    monkeypatch.setattr(bootstrap, "_pause_owner", lambda: main)
    _elapse(main, clock)

    # A client sharing the account's pause shares its track pacing.
    assert bootstrap._reserve_track_start() == pytest.approx(30.0)
