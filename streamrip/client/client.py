"""The clients that interact with the streaming service APIs."""

import asyncio
import contextlib
import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp
import aiolimiter

from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .downloadable import Downloadable

logger = logging.getLogger("streamrip")

# Firefox 83 (2020) stood out as ancient next to a real client's user agent.
# Chrome on Windows is the most common desktop UA on the web, so it blends in
# best; bump the version number every so often to keep it current.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

# Fail a stalled connection after 30s of silence, not aiohttp's default 5 minutes.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)

# Rate limiting and server errors are worth another attempt; other 4xx are not.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_API_ATTEMPTS = 4
MAX_RETRY_DELAY = 60
RATE_LIMIT_PAUSE = 10  # seconds per attempt when a 429 has no Retry-After

T = TypeVar("T")


def retry_after(resp, attempt: int) -> float:
    """How long a 429 asks us to wait: its Retry-After, capped, or a default."""
    try:
        seconds = max(float(resp.headers["Retry-After"]), 1.0)
    except KeyError, ValueError:
        seconds = RATE_LIMIT_PAUSE * attempt
    return min(seconds, MAX_RETRY_DELAY)


class RequestClient:
    """Shared request pacing and retries for API and bootstrap clients."""

    source: str
    session: aiohttp.ClientSession
    rate_limiter: aiolimiter.AsyncLimiter | contextlib.nullcontext
    # time.monotonic() before which no API request is sent (set by a 429)
    _retry_at: float = 0.0

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

    def _pause_owner(self) -> "RequestClient":
        """The client whose 429 pause this one shares (itself, by default)."""
        return self

    def _pause_requests(self, seconds: float):
        """Hold back every request of this account for seconds after a 429."""
        owner = self._pause_owner()
        now = time.monotonic()
        if owner._retry_at <= now:
            logger.warning(
                f"{self.source.capitalize()} is rate limiting us (HTTP 429); "
                f"pausing for {seconds:.0f}s"
            )
        owner._retry_at = max(owner._retry_at, now + seconds)

    async def _get_with_retries(
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
            if (pause := owner._retry_at - time.monotonic()) > 0:
                # + jitter: every request blocked on the same 429 wakes at the
                # same instant otherwise, and immediately re-trips it together.
                await asyncio.sleep(pause + random.random() * 2)
                continue

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
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
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


def new_session(
    verify_ssl: bool = True, timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT
) -> aiohttp.ClientSession:
    """An HTTP session; every request streamrip makes goes through one."""
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
