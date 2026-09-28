import asyncio
from contextlib import nullcontext

from ..config import DownloadsConfig

_unlimited = nullcontext()
_global_semaphore: tuple[int, asyncio.Semaphore] | None = None


def global_download_semaphore(c: DownloadsConfig) -> asyncio.Semaphore | nullcontext:
    """A global semaphore that limits how many tracks download at once.

    That's `max_connections` (1: one after the other); 0 or less means no
    limit. Since it is global, only one value is allowed per session.
    """
    global _global_semaphore

    max_connections = c.max_connections
    if max_connections <= 0:
        return _unlimited

    if _global_semaphore is None:
        _global_semaphore = (max_connections, asyncio.Semaphore(max_connections))

    assert max_connections == _global_semaphore[0], (
        f"Already have other global semaphore {_global_semaphore}"
    )

    return _global_semaphore[1]
