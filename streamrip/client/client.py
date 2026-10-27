"""The clients that interact with the streaming service APIs."""

import asyncio
import contextlib
import json
import logging
import math
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
lazy from collections import Counter
lazy from collections.abc import Awaitable, Callable
lazy from datetime import datetime
lazy from pathlib import Path

lazy import aiohttp
lazy import aiolimiter
lazy import weakref

from ..console import plural
from ..exceptions import AccountHalt
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
lazy from ..config import Config
lazy from .downloadable import Downloadable

logger = logging.getLogger("streamrip")

# --- the browser streamrip presents itself as --------------------------
#
# Every request claims to be a current desktop Chrome, complete with the
# low-entropy Client Hints a real one sends beside its user agent. Which
# Chrome it is rotates every so often, so a long run does not show a
# service one fixed fingerprint. Within one identity nothing contradicts
# anything: the Sec-CH-UA brand list is computed from the user agent's
# major version the way Chromium does it, so hints and user agent age
# together.

# Seconds one identity serves: a uniform pick between these, so the swap
# falls into no rhythm.
ROTATE_AFTER = (45 * 60, 110 * 60)

# Chromium's Sec-CH-UA recipe: a greasy filler brand (name and version
# picked from the major version), Chromium, and the browser's own brand,
# arranged in the order that major version gets.
_GREASE_CHARS = " (:-./);=?_"
_GREASE_VERSIONS = ("8", "99", "24")
_BRAND_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))

# What a web player's fetch of its own API sends; the services' API hosts
# all sit on the site the player runs on.
_FETCH_HEADERS = {
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

# The frozen user agent tokens: Windows 10 and 11 both say 10.0, every
# Mac says 10_15_7, and the versions after the major one are always 0.
_UA_PLATFORMS = {
    "Windows": "Windows NT 10.0; Win64; x64",
    "macOS": "Macintosh; Intel Mac OS X 10_15_7",
}


def _sec_ch_ua(major: int, brand: str = "Google Chrome") -> str:
    """The Sec-CH-UA header for a Chrome `major` version."""
    grease = (
        f"Not{_GREASE_CHARS[major % 11]}A{_GREASE_CHARS[(major + 1) % 11]}Brand"
    )
    brands = (
        f'"{grease}";v="{_GREASE_VERSIONS[major % 3]}"',
        f'"Chromium";v="{major}"',
        f'"{brand}";v="{major}"',
    )
    placed = [None, None, None]
    for position, entry in zip(_BRAND_ORDERS[major % 6], brands, strict=True):
        placed[position] = entry
    return ", ".join(placed)


def _chrome(
    major: int, platform: str, accept_language: str, brand: str = "Google Chrome"
) -> dict[str, str]:
    """One coherent identity: what it says it is, everywhere it says it."""
    return {
        "User-Agent": (
            f"Mozilla/5.0 ({_UA_PLATFORMS[platform]}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        ),
        "Sec-CH-UA": _sec_ch_ua(major, brand),
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": f'"{platform}"',
        "Accept-Language": accept_language,
    }


# Chrome 156 is the stable release (late October 2026), 155 lingers with
# late updaters, and Brave sends its own brand in the same list. When 157
# goes fully stable, move a slot or two from 156 to it. A repeated
# identity weighs heavier in the pick.
_IDENTITIES: tuple[dict[str, str], ...] = (
    _chrome(156, "Windows", "en-US,en;q=0.9"),
    _chrome(156, "Windows", "en-US,en;q=0.9"),
    _chrome(156, "Windows", "en-US,en;q=0.9"),
    _chrome(156, "Windows", "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7"),
    _chrome(156, "macOS", "en-US,en;q=0.9"),
    _chrome(156, "macOS", "en-US,en;q=0.9"),
    _chrome(155, "Windows", "en-US,en;q=0.9"),
    _chrome(155, "Windows", "en-US,en;q=0.9"),
    _chrome(155, "macOS", "en-GB,en;q=0.9"),
    _chrome(156, "Windows", "en-US,en;q=0.9", brand="Brave"),
)

# The identity of the moment, and when it gives way. _rotate_at starts at
# 0 so the first use picks one.
_identity: dict[str, str] = {}
_rotate_at = 0.0
# Live sessions presenting the identity, and plain header dicts kept on
# it (see register_header_dict). Sessions are held weakly: a closed one
# drops out of the rotation on its own.
_browser_sessions = weakref.WeakSet()
_header_dicts: list[dict[str, str]] = []


def _pick_identity() -> dict[str, str]:
    """Another identity than the current one."""
    return random.choice(
        [identity for identity in _IDENTITIES if identity != _identity]
    )


def _apply_identity():
    """Move every live session and registered header dict onto the current
    identity."""
    for session in _browser_sessions:
        session.headers.update(_identity)
    for headers in _header_dicts:
        # API calls only, so the fetch headers come along. The account's
        # own language, set at login, is the truer Accept-Language, so
        # the identity's is left out rather than written over it.
        headers.update(
            {k: v for k, v in _identity.items() if k != "Accept-Language"}
        )
        headers.update(_FETCH_HEADERS)


def _current_identity() -> dict[str, str]:
    """The identity of the moment, swapped for another when its time is up."""
    global _identity, _rotate_at
    if time.monotonic() >= _rotate_at:
        _identity = _pick_identity()
        _rotate_at = time.monotonic() + random.uniform(*ROTATE_AFTER)
        _apply_identity()
    return _identity


def browser_headers() -> dict[str, str]:
    """The current identity's headers, a copy ready to send."""
    return dict(_current_identity())


def api_headers(headers: dict | None = None) -> dict[str, str]:
    """The current identity plus what a web player's API fetch carries, with
    `headers` (an authorization, say) on top of that."""
    return {**_current_identity(), **_FETCH_HEADERS, **(headers or {})}


def register_header_dict(headers: dict[str, str]):
    """Keep a plain header dict on the rotating identity: deezer-py's, which
    its API and GW calls read per request. See _apply_identity for what it
    gets."""
    _header_dicts.append(headers)
    _current_identity()
    _apply_identity()

# Rate limiting and server errors are worth another attempt; other 4xx are not.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_API_ATTEMPTS = 4
MAX_RETRY_DELAY = 60
RATE_LIMIT_PAUSE = 10  # seconds per attempt when a 429 has no Retry-After
# After a 429 the account's requests are spread out: one per SLOWDOWN_START
# seconds, doubled by every further 429 up to SLOWDOWN_MAX, and eased off again
# by each request that goes through.
SLOWDOWN_START = 2
SLOWDOWN_MAX = 30
SLOWDOWN_EASING = 0.98
# What Tidal does, read from two long runs of one account (2026-10-26/27:
# 9,868 tracks at about 550 an hour, then 1,087 at 80 an hour at most). The
# other services' limits are unknown, so these numbers serve them too.
#  - It counts an account's API requests per clock hour (UTC) and starts over
#    on the hour: in 16 rate-limited hours, not one HTTP 429 came in :00-:09.
#  - Past roughly 450 tracks' worth of requests in an hour it refuses some
#    with HTTP 429, more and more towards the end of the hour. It counts
#    requests, not tracks: the first 429 came after 208 to 633 tracks, as a
#    hi-res release takes more requests than a CD one. A 429 carries no
#    Retry-After; 10 to 40 s later the request mostly gets through.
#  - An account that went over that limit 16 hours out of 18 (875 429s,
#    about 10,000 tracks) was suspended with no further warning: its token
#    endpoint answers 403 "abuse_detected" (sub_status 12001). The 429s had
#    not grown more frequent before.
#  - 80 track starts an hour, spread out, for 18 hours: not a single 429.
#
# A first 429 is a sign to slow down: auto turns slow at it, and with auto or
# slow 18 hours brought not one. The suspended account had about 55 an hour,
# 16 hours long. So at this many separate ones within an hour streamrip
# stops, says why, and advises COOLDOWN of rest. Advice, not enforced: only a
# run while they are still within the hour stops at once.
RATE_LIMIT_BUDGET = 10
RATE_LIMIT_WINDOW = 3600
COOLDOWN = 3 * 3600
# Tracks in one clock hour after which Tidal's 429s began at the earliest
# (208): from about here it refuses.
REFUSED_FROM = 200
# streamrip's line, a margin under REFUSED_FROM. The load shows the hour's
# tracks against it, and at full speed each hour that crosses it is warned
# of, louder from the second within FAST_WINDOW: hours on end over it are
# what got the account suspended.
HOURLY_DANGER = 175
# The paces (config.PACES). fast: tracks start one after the other. slow: the
# gaps and the hourly cap below, which ran 18 hours without a 429. auto: fast
# for a small job -- when the tracks it brings and those the account started
# in the last FAST_WINDOW stay within FAST_BUDGET -- and slow from the first
# track for anything bigger. On the way auto turns slow once the budget is
# spent, or at the first 429.
FAST_BUDGET = 100
FAST_WINDOW = 6 * 3600
# The slow pace. Between two track starts (not requests) is a person-shaped
# gap: a log-normal one, mostly tens of seconds, now and then a few minutes.
# Sigma 0.75 is the shape of a person's attention, and mu is what makes
# the mean come out at TRACK_GAP_MEAN seconds (exp(mu + sigma²/2)).
TRACK_GAP_MEAN = 30
TRACK_GAP_SIGMA = 0.75
TRACK_GAP_MU = math.log(TRACK_GAP_MEAN) - TRACK_GAP_SIGMA**2 / 2
# Though nobody's coffee break lasts half an hour mid-album.
TRACK_GAP_MAX = 300
# Every 429 widens the track gaps by this much, up to a limit; every track
# that starts without one narrows them back.
TRACK_GAP_WIDENING = 1.5
TRACK_GAP_WIDENING_MAX = 10
TRACK_GAP_EASING = 0.99
# However human the gaps, at most this many tracks start in any hour, under
# what the service doles out per hour, so its budget never sets the pace.
TRACKS_PER_HOUR = 80


def retry_after(resp, attempt: int) -> float:
    """How long a 429 asks us to wait: its Retry-After, capped, or a default."""
    try:
        seconds = max(float(resp.headers["Retry-After"]), 1.0)
    except KeyError, ValueError:
        seconds = RATE_LIMIT_PAUSE * attempt
    return min(seconds, MAX_RETRY_DELAY)


def _within(times, now: float, window: float = RATE_LIMIT_WINDOW) -> tuple[float, ...]:
    """The times less than window before now, later ones included."""
    return tuple(t for t in times if now - t < window)


def _hours_over(times) -> int:
    """How many clock hours among times have HOURLY_DANGER track starts or more."""
    per_hour = Counter(int(t // 3600) for t in times)
    return sum(n >= HOURLY_DANGER for n in per_hour.values())


def _danger(name: str) -> str:
    """What HOURLY_DANGER means, the same in every message that warns of it."""
    return (
        f"From about {REFUSED_FROM} tracks an hour {name} starts refusing "
        "requests, and keeping that up gets an account banned: streamrip draws "
        f"the line at {HOURLY_DANGER}."
    )


@dataclass(frozen=True, slots=True)
class Load:
    """How hard an account has been working its service: what a run's
    summary shows, and what its risk is (see the notes on Tidal above)."""

    pace: str
    slow: bool
    # tracks started this clock hour, the one the service counts in
    this_hour: int
    # tracks started in the last FAST_WINDOW
    recent: int
    # separate 429s in the last hour
    limited: int
    # seconds until the next track's turn, 0 when none is waiting for one
    next_in: float = 0.0
    # a job auto would pace: more than FAST_BUDGET tracks in FAST_WINDOW,
    # those before it included
    big: bool = False
    # clock hours in FAST_WINDOW with HOURLY_DANGER tracks or more
    hours_over: int = 0

    @property
    def risk(self) -> str:
        """low, medium, high or very high. High past HOURLY_DANGER this hour
        or from a second 429; very high twice that, a second hour over it, or
        halfway to the RATE_LIMIT_BUDGET that stops a run; medium on the way,
        after a 429, or at full speed on a big job."""
        if (
            self.hours_over >= 2
            or self.this_hour >= 2 * HOURLY_DANGER
            or self.limited >= RATE_LIMIT_BUDGET // 2
        ):
            return "very high"
        if self.this_hour >= HOURLY_DANGER or self.limited >= 2:
            return "high"
        if (
            self.limited
            or self.this_hour >= HOURLY_DANGER // 2
            or (self.big and not self.slow)
        ):
            return "medium"
        return "low"


class RequestClient:
    """Shared request and track-start pacing, and retries, for API and
    bootstrap clients."""

    source: str
    session: aiohttp.ClientSession
    rate_limiter: aiolimiter.AsyncLimiter | contextlib.nullcontext
    # time.monotonic() before which no API request is sent (set by a 429)
    _retry_at: float = 0.0
    # seconds between requests while the account is being rate limited, and
    # the time.monotonic() at which the next request may go out
    _spacing: float = 0.0
    _next_slot: float = 0.0
    # When the 429s of the last RATE_LIMIT_WINDOW began, and the halt that
    # stops this run, if any. Wall clock (time.time()), not monotonic like
    # _retry_at: the times outlive a run, in a file next to the config, so
    # every run of the account shares one count.
    _limited_at: tuple[float, ...] = ()
    _halt: AccountHalt | None = None
    # Track-start pacing (before_track): how much the 429s have widened the
    # gaps, the starts of the last FAST_WINDOW (reserved ones included, so
    # the last is the latest), and whether the last hour is full. The first
    # two outlive a run, wall clock too.
    _track_gap_scale: float = 1.0
    _track_starts: tuple[float, ...] = ()
    _track_hour_full: bool = False
    # The pace asked for (config.PACES), whether the account goes at the
    # slow one now, and the clock hour fast was last warned about.
    _pace: str = "auto"
    _slow: bool = False
    _danger_warned: float | None = None
    _big: bool = False
    # the clock hour whose tracks the log has yet to be told (_report_hour)
    _hour_reported: float | None = None
    # whether this run has picked up the pacing the last one left
    _pacing_state_loaded: bool = False

    @staticmethod
    def get_rate_limiter(
        requests_per_min: int,
    ) -> aiolimiter.AsyncLimiter | contextlib.nullcontext:
        # One request every 60/n seconds, not "n per 60 seconds": the latter
        # is a bucket n deep, so a fresh start (or the end of a 429 pause,
        # once it has refilled) lets up to n requests out at the same
        # instant -- exactly the burst that trips the limit in the first place.
        return (
            aiolimiter.AsyncLimiter(1, 60 / requests_per_min)
            if requests_per_min > 0
            else contextlib.nullcontext()
        )

    def _pause_owner(self) -> RequestClient:
        """The client whose 429 pause this one shares (itself, by default)."""
        return self

    async def _renew_login(self) -> bool:
        """Renew a login that lapsed mid-run; True if there is a fresh one.

        Only Tidal's access token expires while a rip runs; every other
        source returns False and the 401 reaches the caller as before.
        """
        return False

    def _pacing_state_path(self) -> Path | None:
        """The file this account's pacing outlives a run in: next to the
        config, so a config of one's own (a ripper's, say) paces its own
        account.

        None for a client without a config, like Qobuz's bootstrap scraper:
        it shares its owner's pacing.
        """
        config = getattr(self, "global_config", None)
        if config is None:
            return None
        return config.path.parent / f"pacing-{self.source}.json"

    def _load_pacing_state(self):
        """Pick up this account's pacing where the last run left it.

        Only the last hour of it counts, and an hour without a track start
        or a 429 is a fresh start, widened gaps and all: the wait was the
        cooldown. A run that finds the rate-limit budget spent stops before
        its first request.
        """
        path = self._pacing_state_path()
        if path is None or not path.is_file():
            return
        now = time.time()
        try:
            state = json.loads(path.read_text())
            self._track_starts = _within(state["track_starts"], now, FAST_WINDOW)
            self._limited_at = _within(state["limited_at"], now)
            if _within(self._track_starts, now) or self._limited_at:
                self._track_gap_scale = state["track_gap_scale"]
        except OSError, TypeError, ValueError, KeyError:
            # Unreadable is a fresh start, not a failed rip.
            # (json.JSONDecodeError is a ValueError.)
            logger.debug("Could not read the pacing state at %s", path)
            return
        if len(self._limited_at) >= RATE_LIMIT_BUDGET:
            self._halt = self._rate_limit_halt()

    def _save_pacing_state(self):
        """Leave this account's pacing for the next run.

        Replaced in one step, like the config: a write cut short must not
        read as a fresh start, least of all when what it would forget is a
        halt.
        """
        path = self._pacing_state_path()
        if path is None:
            return
        now = time.time()
        state = {
            "track_starts": _within(self._track_starts, now, FAST_WINDOW),
            "track_gap_scale": self._track_gap_scale,
            "limited_at": _within(self._limited_at, now),
        }
        tmp = path.with_name(f".{path.name}-{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(state))
            tmp.replace(path)
        except OSError:
            # A rip must not fail over its state file.
            logger.debug("Could not save the pacing state to %s", path)

    def _paced_owner(self) -> RequestClient:
        """The pause owner, with the pacing the last run left picked up."""
        owner = self._pause_owner()
        if not owner._pacing_state_loaded:
            owner._pacing_state_loaded = True
            owner._load_pacing_state()
        return owner

    def pace(self, pace: str, planned: int):
        """Set the account's pace (config.PACES) for a run that brings
        `planned` tracks, and say what it is and why."""
        owner = self._paced_owner()
        name = self.source.capitalize()
        recent = len(_within(owner._track_starts, time.time(), FAST_WINDOW))
        big = planned + recent > FAST_BUDGET
        owner._pace, owner._big = pace, big
        owner._slow = pace == "slow" or (pace == "auto" and big)
        steady = f"a steady pace of about {TRACKS_PER_HOUR} tracks an hour"
        if pace == "fast":
            # Asked for, so said either way, and loudly when it is a lot.
            (logger.warning if big else logger.info)(
                "%s: full speed for %s, as asked (--pace fast): no waiting "
                "between tracks%s",
                name,
                plural(planned, "track"),
                f", so don't keep it up for long. {_danger(name)}" if big else ".",
            )
        elif pace == "slow":
            logger.info(
                "%s: %s at %s, as asked (--pace slow).",
                name,
                plural(planned, "track"),
                steady,
            )
        elif owner._slow:
            logger.info(
                "%s: %s to download%s, so at %s. %s Add --pace fast to rip "
                "without waiting between tracks.",
                name,
                plural(planned, "track"),
                f" and {recent} in the last {FAST_WINDOW // 3600} hours"
                if recent
                else "",
                steady,
                _danger(name),
            )
        else:
            logger.info(
                "%s: %s to download, a small job, so at full speed.",
                name,
                plural(planned, "track"),
            )

    def _turn_slow(self, why: str):
        """Turn an auto pace slow for the rest of the run, saying why."""
        name = self.source.capitalize()
        self._slow = True
        logger.warning(
            "%s: %s, so from here on at a steady pace of about %s tracks an hour. %s",
            name,
            why,
            TRACKS_PER_HOUR,
            _danger(name),
        )

    def _warn_over(self, hours: int):
        """Say that full speed crossed HOURLY_DANGER this hour, the `hours`th
        time within FAST_WINDOW: in red from the second, which is how the
        suspended account went."""
        name = self.source.capitalize()
        if hours < 2:
            logger.warning(
                "%s: %s tracks this hour at full speed, where streamrip draws the "
                "line. Don't keep this up: hours over it are what got an account "
                "banned. Stop and rerun with --pace slow to go on safely.",
                name,
                HOURLY_DANGER,
            )
        else:
            logger.error(
                "%s: over %s tracks an hour at full speed in %s of the last %s "
                "hours. Hour after hour over the line is how an account got "
                "banned. Stop now, and rerun with --pace slow to go on safely.",
                name,
                HOURLY_DANGER,
                hours,
                FAST_WINDOW // 3600,
            )

    def _report_hour(self, starts: tuple[float, ...], clock_hour: float):
        """Log the last clock hour's tracks and rate limits once it is over:
        the unit the service counts in, and a record of a long run without
        progress bars."""
        last = self._hour_reported
        self._hour_reported = clock_hour
        if last is None or last >= clock_hour:
            return
        started = sum(1 for t in starts if last <= t < clock_hour)
        limited = sum(1 for t in self._limited_at if last <= t < clock_hour)
        if started or limited:
            logger.info(
                "%s: %s and %s in the hour up to %s (%s).",
                self.source.capitalize(),
                plural(started, "track"),
                plural(limited, "rate limit") if limited else "no rate limits",
                # Local time, as the log's own timestamps: a whole hour in
                # UTC is one here too, where the offset is whole hours.
                datetime.fromtimestamp(clock_hour).strftime("%H:%M"),
                "steady pace" if self._slow else "full speed",
            )

    def load(self) -> Load:
        """How hard the account has been working its service (see Load)."""
        owner = self._paced_owner()
        now = time.time()
        started = [t for t in owner._track_starts if t <= now]
        waiting = [t for t in owner._track_starts if t > now]
        recent = _within(started, now, FAST_WINDOW)
        return Load(
            pace=owner._pace,
            slow=owner._slow,
            this_hour=sum(1 for t in started if t >= now - now % 3600),
            recent=len(recent),
            limited=len(_within(owner._limited_at, now)),
            next_in=min(waiting) - now if waiting else 0.0,
            big=owner._big or len(recent) > FAST_BUDGET,
            hours_over=_hours_over(recent),
        )

    def time_for(self, tracks: int) -> float | None:
        """About how long `tracks` more tracks take to start at the steady
        pace: TRACK_GAP_MEAN apart (wider after 429s) while the hour has room,
        then TRACKS_PER_HOUR an hour. None at full speed, where the downloads
        themselves set the time."""
        owner = self._paced_owner()
        if not owner._slow:
            return None
        gap = TRACK_GAP_MEAN * owner._track_gap_scale
        room = max(TRACKS_PER_HOUR - len(_within(owner._track_starts, time.time())), 0)
        first, rest = min(tracks, room), max(tracks - room, 0)
        return first * gap + rest * max(gap, RATE_LIMIT_WINDOW / TRACKS_PER_HOUR)

    def _rate_limit_halt(self) -> AccountHalt:
        """The stop at RATE_LIMIT_BUDGET, with the rest it advises."""
        name = self.source.capitalize()
        until = datetime.fromtimestamp(max(self._limited_at) + COOLDOWN)
        return AccountHalt(
            f"Paused to protect your account: {name} returned HTTP 429 (rate "
            f"limited) {len(self._limited_at)} times in the last hour. Pushing "
            "through risks a ban, so this run stops here. Best to wait about "
            f"{COOLDOWN // 3600} hours, until around {until:%H:%M}: in the "
            f"meantime, listen to music normally in {name}'s own app — that "
            "generates realistic listening traffic and helps your account "
            "recover. This advice isn't enforced: a new run only stops "
            f"immediately while it still sees {RATE_LIMIT_BUDGET} rate limits "
            "in the past hour; after that it rips again, even before "
            f"{until:%H:%M}. When you do restart, anything already downloaded "
            "is skipped."
        )

    def _say_limited(self, count: int, seconds: float):
        """Say the `count`th 429 of the last hour and the wait it brings,
        louder as RATE_LIMIT_BUDGET comes near."""
        owner = self._paced_owner()
        name = self.source.capitalize()
        wait = (
            f"pausing {seconds:.0f}s, then one request every {owner._spacing:.0f}s "
            f"and tracks about {TRACK_GAP_MEAN * owner._track_gap_scale:.0f}s apart"
        )
        if count == 1:
            logger.warning(
                "%s: first rate limit (HTTP 429) detected. Let's wait a bit: %s.",
                name,
                wait,
            )
        elif count < RATE_LIMIT_BUDGET // 2:
            logger.warning(
                "%s: rate limit %s of %s in the last hour (at %s streamrip stops): %s.",
                name,
                count,
                RATE_LIMIT_BUDGET,
                RATE_LIMIT_BUDGET,
                wait,
            )
        else:
            logger.error(
                "%s: not good, rate limit %s of %s in the last hour. Too many rate "
                "limits: going on like this gets an account banned, and at %s "
                "streamrip stops. Now %s.",
                name,
                count,
                RATE_LIMIT_BUDGET,
                RATE_LIMIT_BUDGET,
                wait,
            )

    def _pause_requests(self, seconds: float):
        """Hold back every request of this account for seconds after a 429."""
        owner = self._paced_owner()
        now = time.monotonic()
        if owner._retry_at <= now:
            # A new 429 episode: it counts towards the budget, and spreads out
            # both the requests and the track starts that follow.
            wall = time.time()
            owner._limited_at = (*_within(owner._limited_at, wall), wall)
            if owner._pace == "auto" and not owner._slow:
                owner._turn_slow("it is refusing requests (HTTP 429)")
            if len(owner._limited_at) >= RATE_LIMIT_BUDGET:
                owner._halt = owner._rate_limit_halt()
                # Saved before the raise, so the next run halts too.
                owner._save_pacing_state()
                raise owner._halt
            owner._spacing = min(max(owner._spacing * 2, SLOWDOWN_START), SLOWDOWN_MAX)
            owner._track_gap_scale = min(
                owner._track_gap_scale * TRACK_GAP_WIDENING, TRACK_GAP_WIDENING_MAX
            )
            self._say_limited(len(owner._limited_at), seconds)
        owner._retry_at = max(owner._retry_at, now + seconds)
        owner._save_pacing_state()

    async def before_track(self) -> None:
        """Wait until this track may start, at the account's pace.

        Back to back, tracks wore out the service's hourly budget at the
        same point of every hour, hour after hour, until the account was
        suspended. So at the slow pace starts are TRACK_GAP_MEAN seconds
        apart on average (log-normal, capped at TRACK_GAP_MAX), wider after
        every 429, and at most TRACKS_PER_HOUR in any hour; at the fast pace
        they follow each other. The first track after a quiet hour starts at
        once, and the pacing outlives a run: every run of the account shares
        it, so a ripper starting streamrip job after job counts as one run.
        """
        wait = self._reserve_track_start()
        if wait:
            await asyncio.sleep(wait)

    def _reserve_track_start(self) -> float:
        """Reserve the next track start; return how long until it.

        The caller does the waiting, so tracks holding a reserved start
        don't block each other.
        """
        owner = self._paced_owner()
        now = time.time()
        starts = _within(owner._track_starts, now, FAST_WINDOW)
        owner._report_hour(starts, now - now % 3600)
        if owner._pace == "auto" and not owner._slow and len(starts) >= FAST_BUDGET:
            owner._turn_slow(
                f"{len(starts)} tracks in the last {FAST_WINDOW // 3600} hours"
            )
        start = now
        if owner._slow:
            hour = _within(starts, now)
            if hour:
                gap = min(
                    random.lognormvariate(TRACK_GAP_MU, TRACK_GAP_SIGMA), TRACK_GAP_MAX
                )
                start = max(now, hour[-1]) + gap * owner._track_gap_scale
            full = len(hour) >= TRACKS_PER_HOUR
            if full:
                # An hour after the TRACKS_PER_HOUR-th latest start, not the
                # oldest: reserved starts count too, or tracks resolving side
                # by side slip past the cap.
                start = max(start, hour[-TRACKS_PER_HOUR] + RATE_LIMIT_WINDOW)
                if not owner._track_hour_full:
                    logger.warning(
                        "%s tracks started in the last hour, holding the next one "
                        "for %.0fs",
                        TRACKS_PER_HOUR,
                        start - now,
                    )
            owner._track_hour_full = full
        elif (clock_hour := now - now % 3600) != owner._danger_warned:
            # fast: said once an hour when it crosses the line
            if sum(1 for t in starts if t >= clock_hour) + 1 >= HOURLY_DANGER:
                owner._danger_warned = clock_hour
                self._warn_over(1 + _hours_over(starts))
        owner._track_starts = (*starts, start)
        # A track that starts with no 429 since the last one eases the gaps.
        owner._track_gap_scale = max(owner._track_gap_scale * TRACK_GAP_EASING, 1.0)
        owner._save_pacing_state()
        return start - now

    async def _get_with_retries[T](
        self,
        url: str,
        read: Callable[[aiohttp.ClientResponse], Awaitable[T]],
        params: dict | None = None,
        headers: dict | None = None,
    ) -> T:
        """GET an API url, retrying what is likely to work a moment later.

        Connection errors, timeouts, 5xx and 429 are retried with backoff, up to
        MAX_API_ATTEMPTS. A 429 holds back every request, not just this one,
        since the rate limit belongs to the account. A 401 renews the login
        once (_renew_login) and sends this request again. `read` turns the
        response into the result; it gets every response that isn't retried,
        including the last attempt's, whatever its status.
        """
        attempt = 0
        renewed = False
        while True:
            # Paced before the halt check: artist and album requests go out
            # before any track, and a halt the last run left has to stop this
            # one at its very first request.
            owner = self._paced_owner()
            if owner._halt is not None:
                raise owner._halt
            if (pause := owner._retry_at - time.monotonic()) > 0:
                # + jitter: every request blocked on the same 429 wakes at the
                # same instant otherwise, and immediately re-trips it together.
                await asyncio.sleep(pause + random.random() * 2)
                continue

            if owner._spacing:
                now = time.monotonic()
                wait = max(owner._next_slot - now, 0.0)
                owner._next_slot = now + wait + owner._spacing
                if wait:
                    await asyncio.sleep(wait)

            delay = 0.0
            try:
                async with self.rate_limiter:
                    # A sibling request may have extended the pause while we
                    # waited for admission. Discard this slot and wait outside
                    # the limiter, then acquire a fresh slot before sending.
                    if owner._retry_at > time.monotonic():
                        continue
                    attempt += 1
                    async with self.session.get(
                        # api_headers also swaps in the next identity when
                        # the current one's time is up.
                        url, params=params, headers=api_headers(headers)
                    ) as resp:
                        if resp.status == 429:
                            self._pause_requests(retry_after(resp, attempt))
                        elif resp.status < 400 and owner._spacing:
                            owner._spacing *= SLOWDOWN_EASING
                            if owner._spacing < 0.5:
                                owner._spacing = 0.0
                        if (
                            resp.status == 401
                            and not renewed
                            and await self._renew_login()
                        ):
                            # A login that lapsed mid-run (Tidal's token lives
                            # hours) is renewed once, and this request is sent
                            # again with the fresh one.
                            renewed = True
                            continue
                        if (
                            resp.status not in RETRY_STATUSES
                            or attempt == MAX_API_ATTEMPTS
                        ):
                            return await read(resp)
                        if resp.status != 429:
                            reason = f"HTTP {resp.status}"
                            delay = 2**attempt + random.random()
            except aiohttp.ClientSSLError:
                raise
            except (
                TimeoutError,
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
            ) as e:
                if attempt == MAX_API_ATTEMPTS:
                    raise
                reason = type(e).__name__
                delay = 2**attempt + random.random()

            if delay:
                # The reason only: the url can carry credentials (Qobuz).
                logger.warning(
                    "%s request failed (%s), retrying in %.0fs",
                    self.source.capitalize(),
                    reason,
                    delay,
                )
                await asyncio.sleep(delay)

    async def _get_text_with_retries(self, url: str) -> str:
        """Fetch a web page or script with retries and reject HTTP errors."""

        async def read(resp):
            resp.raise_for_status()
            return await resp.text(encoding="utf-8")

        return await self._get_with_retries(url, read)

    async def _get_json(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> tuple[int, Any]:
        """GET an API url with retries: (status, JSON body), whatever the status.

        A body without JSON -- an HTML error page, the empty body of a 401 --
        comes back as {"message": ...}, so the caller can report the status.
        Never aiohttp's ContentTypeError: it quotes the whole url, which can
        carry credentials (Qobuz's user_auth_token).
        """

        async def read(resp) -> tuple[int, Any]:
            try:
                body = await resp.json(content_type=None)
            except ValueError:  # json.JSONDecodeError is one
                body = None
            if body is None:
                body = {"message": f"non-JSON response ({resp.content_type})"}
            return resp.status, body

        return await self._get_with_retries(url, read, params, headers)


class Client(RequestClient, ABC):
    """A streaming service: logging in, metadata, search and downloads.

    Every client keeps the whole Config as global_config and its own section
    of it ([tidal], [qobuz], ...) as config.
    """

    max_quality: int

    def __init__(self, config: Config):
        self.global_config = config
        self.config = getattr(config.session, self.source)
        self.logged_in = False
        self.rate_limiter = self.get_rate_limiter(
            config.session.downloads.requests_per_minute
        )

    def _ensure_session(self):
        """Open the HTTP session, unless there is an open one."""
        if getattr(self, "session", None) is None or self.session.closed:
            self.session = new_session(
                verify_ssl=self.global_config.session.downloads.verify_ssl
            )

    async def close(self):
        """Close the HTTP session, if there is one."""
        if getattr(self, "session", None) is not None:
            await self.session.close()

    @abstractmethod
    async def login(self):
        raise NotImplementedError

    @abstractmethod
    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def search(self, media_type: str, query: str, limit: int) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    async def get_downloadable(self, item_id: str, quality: int) -> Downloadable:
        raise NotImplementedError

    async def get_lyrics(self, track_id: str, plain: bool) -> str:
        """A track's lyrics from the source, plain or else synced (LRC) where
        there are; "" when it has none. Only Tidal sends any.
        """
        return ""


def new_session(
    verify_ssl: bool = True,
    timeout: aiohttp.ClientTimeout | None = None,
    user_agent: str | None = None,
) -> aiohttp.ClientSession:
    """An HTTP session; every request streamrip makes goes through one.

    It presents the current browser identity and follows it as identities
    rotate. A `user_agent` (LRCLIB's, which says what is really asking)
    replaces the identity outright.
    """
    if timeout is None:
        # Fail a stalled connection after 30s of silence, not aiohttp's default 5 minutes.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)
    connector = aiohttp.TCPConnector(
        **get_aiohttp_connector_kwargs(verify_ssl=verify_ssl),
        resolver=aiohttp.ThreadedResolver(),
    )
    # trust_env: honour HTTP(S)_PROXY / ALL_PROXY like requests already
    # does for the audio downloads (upstream #961).
    session = aiohttp.ClientSession(
        headers=browser_headers() if user_agent is None else {"User-Agent": user_agent},
        connector=connector,
        timeout=timeout,
        trust_env=True,
    )
    if user_agent is None:
        _browser_sessions.add(session)
    return session
