import asyncio
import logging
from abc import ABC, abstractmethod

from ..exceptions import TrackDownloadFailedError

logger = logging.getLogger("streamrip")


class Media(ABC):
    async def rip(self):
        await self.preprocess()
        await self.download()
        await self.postprocess()

    @abstractmethod
    async def preprocess(self):
        """Create directories, download cover art, etc."""
        raise NotImplementedError

    @abstractmethod
    async def download(self):
        """Download and tag the actual audio files in the correct directories."""
        raise NotImplementedError

    @abstractmethod
    async def postprocess(self):
        """Update database, run conversion, delete garbage files etc."""
        raise NotImplementedError


class Pending(ABC):
    """A request to download a `Media` whose metadata has not been fetched."""

    @abstractmethod
    async def resolve(self) -> Media | None:
        """Fetch metadata and resolve into a downloadable `Media` object."""
        raise NotImplementedError


async def resolve_or_none(pending: Pending) -> Media | None:
    """Resolve one item; a failure is logged and costs only that item."""
    try:
        return await pending.resolve()
    except Exception as e:
        item = getattr(pending, "id", "")
        logger.error(f"Error resolving {item}: {type(e).__name__}: {e}")
        return None


def filter_prefer_explicit(tracks: list) -> list:
    """Drop the clean copy of any track that also has an explicit copy.

    Tracks are grouped by (title, artist); within a group that has both an
    explicit and a non-explicit entry, only the explicit one(s) are kept. A
    group that's all-explicit or all-clean is left untouched -- there's
    nothing to prefer against. Used by Album/Playlist when [metadata]
    prefer_explicit is set, so a catalog that lists both editions of the
    same song doesn't turn into two downloads of it.
    """
    groups: dict[tuple[str, str], list] = {}
    for track in tracks:
        key = (track.meta.title.strip().lower(), track.meta.artist.strip().lower())
        groups.setdefault(key, []).append(track)

    kept = []
    for group in groups.values():
        explicit = [t for t in group if t.meta.info.explicit]
        kept.extend(explicit if 0 < len(explicit) < len(group) else group)
    return kept


async def rip_tracks(pending: list, resolve_concurrency: int, prefer_explicit: bool):
    """Resolve and download the tracks of an album or playlist.

    At most `resolve_concurrency` tracks resolve at once, and each downloads
    as soon as it's resolved, outside that limit (downloads have their own).
    A failure costs only that track. With prefer_explicit, every track is
    resolved first, so a clean copy can be dropped for an explicit one.
    """
    slots = asyncio.Semaphore(resolve_concurrency)

    async def resolve(item):
        async with slots:
            return await resolve_or_none(item)

    async def rip(track):
        try:
            await track.rip()
        except TrackDownloadFailedError:
            pass  # already logged and recorded by Track.download()
        except Exception as e:
            # Include the type: some exceptions have an empty message (#938).
            logger.error(f"Error downloading track: {type(e).__name__}: {e}")

    if prefer_explicit:
        resolved = await asyncio.gather(*map(resolve, pending))
        tracks = [t for t in resolved if t is not None]
        await asyncio.gather(*map(rip, filter_prefer_explicit(tracks)))
        return

    async def resolve_and_rip(item):
        if (track := await resolve(item)) is not None:
            await rip(track)

    await asyncio.gather(*map(resolve_and_rip, pending))
