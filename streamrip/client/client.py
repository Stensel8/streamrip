"""The clients that interact with the streaming service APIs."""

import contextlib
import logging
from abc import ABC, abstractmethod

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
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# Fail a stalled connection after 30s of silence, not aiohttp's default 5 minutes.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)


class Client(ABC):
    source: str
    max_quality: int
    session: aiohttp.ClientSession
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
