"""The clients that interact with the streaming service APIs."""

import asyncio
import contextlib
import logging
import math
import random
import time
from abc import ABC, abstractmethod
lazy from collections.abc import Awaitable, Callable

lazy import aiohttp
lazy import aiolimiter

from ..exceptions import RateLimitHalt
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
lazy from .downloadable import Downloadable

logger = logging.getLogger("streamrip")

# Firefox 83 (2020) stood out as ancient next to a real client's user agent.
# Chrome on Windows is the most common desktop UA on the web, so it blends in
# best; bump the version number every so often to keep it current.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

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
# A few 429s are normal. This many separate ones within an hour are not, and
# going on risks the account, so streamrip stops and says so.
RATE_LIMIT_BUDGET = 40
RATE_LIMIT_WINDOW = 3600
# Between two track starts (not requests) is a person-shaped gap: a
# log-normal one, mostly tens of seconds, now and then a few minutes.
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
    # when the 429s of the last RATE_LIMIT_WINDOW began, and the stop they led to
    _limited_at: tuple[float, ...] = ()
    _halt: RateLimitHalt | None = None
    # Track-start pacing (before_track): how much the 429s have widened the
    # gaps between track starts, the earliest the next one may start, when
    # the starts of the last RATE_LIMIT_WINDOW began, and whether that hour
    # is full right now.
    _track_gap_scale: float = 1.0
    _next_track_start: float = 0.0
    _track_starts: tuple[float, ...] = ()
    _track_hour_full: bool = False

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

    def _pause_requests(self, seconds: float):
        """Hold back every request of this account for seconds after a 429."""
        owner = self._pause_owner()
        now = time.monotonic()
        if owner._retry_at <= now:
            # A new 429 episode: the gaps between track starts widen too, on
            # the account the pause belongs to.
            owner._track_gap_scale = min(
                owner._track_gap_scale * TRACK_GAP_WIDENING, TRACK_GAP_WIDENING_MAX
            )
            owner._limited_at = (
                *(t for t in owner._limited_at if now - t < RATE_LIMIT_WINDOW),
                now,
            )
            if len(owner._limited_at) >= RATE_LIMIT_BUDGET:
                owner._halt = RateLimitHalt(
                    f"Stopped to protect your account: {self.source.capitalize()} "
                    f"has rate limited streamrip {len(owner._limited_at)} times in "
                    "the last hour, and going on risks a suspension. Wait a few "
                    "hours, then run the same command again; what is already "
                    "downloaded is skipped."
                )
                raise owner._halt
            owner._spacing = min(max(owner._spacing * 2, SLOWDOWN_START), SLOWDOWN_MAX)
            logger.warning(
                f"{self.source.capitalize()} is rate limiting us (HTTP 429); "
                f"pausing for {seconds:.0f}s, then one request every "
                f"{owner._spacing:.0f}s and track starts about "
                f"{math.exp(TRACK_GAP_MU) * owner._track_gap_scale:.0f}s apart"
            )
        owner._retry_at = max(owner._retry_at, now + seconds)

    async def before_track(self) -> None:
        """Hold this track back until a person might have started it.

        What gets an account suspended is not any one request but the
        pattern: a track every few seconds, hour after hour, wearing out
        the service's hourly request budget at the same point of every
        hour. The first track of a session starts when the user does; after
        that, log-normal gaps between starts (mean TRACK_GAP_MEAN, capped
        at TRACK_GAP_MAX), widened by every 429 and eased back by every
        track that starts without one. And however human the gaps, at most
        TRACKS_PER_HOUR tracks start in any hour, so the budget never gets
        to set the pace and trip the 429s in the first place.
        """
        wait = self._reserve_track_start()
        if wait:
            await asyncio.sleep(wait)

    def _reserve_track_start(self) -> float:
        """Plan the next track start; return how long to wait for it.

        The wait is spent by the caller, not held here, so any number of
        tracks can hold a reserved start without blocking each other.
        """
        owner = self._pause_owner()
        now = time.monotonic()
        if owner._track_starts:
            gap = min(
                random.lognormvariate(TRACK_GAP_MU, TRACK_GAP_SIGMA), TRACK_GAP_MAX
            )
            start = max(now, owner._next_track_start) + gap * owner._track_gap_scale
        else:
            # The first track of a session starts when the user does.
            start = now
        # Starts an hour old free up room again; the oldest one still in
        # the hour says when the next slot opens.
        owner._track_starts = tuple(
            t for t in owner._track_starts if now - t < RATE_LIMIT_WINDOW
        )
        if len(owner._track_starts) >= TRACKS_PER_HOUR:
            start = max(start, owner._track_starts[0] + RATE_LIMIT_WINDOW)
            if not owner._track_hour_full:
                owner._track_hour_full = True
                logger.warning(
                    f"{TRACKS_PER_HOUR} tracks started in the last hour, "
                    f"holding the next one for {start - now:.0f}s"
                )
        else:
            owner._track_hour_full = False
        owner._track_starts = (*owner._track_starts, start)
        owner._next_track_start = start
        # This track starting without a 429 in between is evidence the
        # pacing is enough again.
        owner._track_gap_scale = max(owner._track_gap_scale * TRACK_GAP_EASING, 1.0)
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
        since the rate limit belongs to the account. `read` turns the response
        into the result; it gets every response that isn't retried, including
        the last attempt's, whatever its status.
        """
        attempt = 0
        while True:
            owner = self._pause_owner()
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
                        url, params=params, headers=headers
                    ) as resp:
                        if resp.status == 429:
                            self._pause_requests(retry_after(resp, attempt))
                        elif resp.status < 400 and owner._spacing:
                            owner._spacing *= SLOWDOWN_EASING
                            if owner._spacing < 0.5:
                                owner._spacing = 0.0
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
                    f"{self.source.capitalize()} request failed ({reason}), "
                    f"retrying in {delay:.0f}s"
                )
                await asyncio.sleep(delay)

    async def _get_text_with_retries(self, url: str) -> str:
        """Fetch a web page or script with retries and reject HTTP errors."""

        async def read(resp):
            resp.raise_for_status()
            return await resp.text(encoding="utf-8")

        return await self._get_with_retries(url, read)


class Client(RequestClient, ABC):
    """Provider interface backed by shared HTTP request handling."""

    max_quality: int
    logged_in: bool

    @abstractmethod
    async def login(self):
        raise NotImplementedError

    @abstractmethod
    async def get_metadata(self, item: str, media_type):
        raise NotImplementedError

    @abstractmethod
    async def search(self, media_type: str, query: str, limit: int = 500) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    async def get_downloadable(self, item: str, quality: int) -> Downloadable:
        raise NotImplementedError

    async def get_lyrics(self, track_id: str, plain: bool) -> str:
        """A track's lyrics from the source, plain or else synced (LRC) where
        there are; "" when it has none. Only Tidal sends any.
        """
        return ""


def new_session(
    verify_ssl: bool = True, timeout: aiohttp.ClientTimeout | None = None
) -> aiohttp.ClientSession:
    """An HTTP session; every request streamrip makes goes through one."""
    if timeout is None:
        # Fail a stalled connection after 30s of silence, not aiohttp's default 5 minutes.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)
    connector = aiohttp.TCPConnector(
        **get_aiohttp_connector_kwargs(verify_ssl=verify_ssl),
        resolver=aiohttp.ThreadedResolver(),
    )
    # trust_env: honour HTTP(S)_PROXY / ALL_PROXY like requests already
    # does for the audio downloads (upstream #961).
    return aiohttp.ClientSession(
        headers={"User-Agent": DEFAULT_USER_AGENT},
        connector=connector,
        timeout=timeout,
        trust_env=True,
    )
