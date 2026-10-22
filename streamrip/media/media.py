import asyncio
import logging
from abc import ABC, abstractmethod

from .. import progress
from ..exceptions import TrackDownloadFailedError

logger = logging.getLogger("streamrip")


class Media(ABC):
    async def rip(self):
        await self.preprocess()
        await self.download()
        await self.postprocess()

    async def preprocess(self):
        """Create directories, download cover art, etc."""

    @abstractmethod
    async def download(self):
        """Download and tag the actual audio files in the correct directories."""
        raise NotImplementedError

    async def postprocess(self):
        """Update database, run conversion, delete garbage files etc."""


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


async def rip_tracks(
    pending: list,
    resolve_concurrency: int,
    prefer_explicit: bool,
    progress_bars: bool = True,
):
    """Look up, resolve and download the tracks of an album or playlist.

    At most `resolve_concurrency` tracks are looked up or resolved at once,
    and each downloads as soon as it's resolved, outside that limit
    (downloads have their own). A failure costs only that track. With
    prefer_explicit, every track is looked up first, so a clean copy can be
    dropped for an explicit one: that only needs their metadata, which on
    Tidal costs no request, as the album or playlist listed its tracks.

    Each track counts on the overall progress bar whatever its outcome. The
    count is added here, the single place album, playlist and (album by album)
    artist downloads all pass through, so the bar is one unified track total.
    A row of its own counts the tracks looked up.
    """
    slots = asyncio.Semaphore(resolve_concurrency)
    looking_up = progress.get_resolve_callback(progress_bars, len(pending))

    async def look_up(item) -> bool:
        """Fetch one item's metadata; False if it's not to download."""
        try:
            return await item.look_up() is not None
        except Exception as e:
            item_id = getattr(item, "id", "")
            logger.error(f"Error looking up {item_id}: {type(e).__name__}: {e}")
            return False
        finally:
            looking_up.update(1)

    async def look_up_only(item) -> bool:
        """look_up, bounded by the concurrency limit."""
        async with slots:
            return await look_up(item)

    async def rip(item, looked_up: bool = True):
        """Resolve one item and download it right away, counting it as done.

        An item not looked up yet is, in the same slot: a slot of its own
        would queue behind every other item's look-up.
        """
        try:
            async with slots:
                if not (looked_up or await look_up(item)):
                    return
                track = await resolve_or_none(item)
            if track is not None:
                await track.rip()
        except TrackDownloadFailedError:
            pass  # already logged and recorded by Track.download()
        except Exception as e:
            # Include the type: some exceptions have an empty message (#938).
            logger.error(f"Error downloading track: {type(e).__name__}: {e}")
        finally:
            progress.overall_advance(enabled=progress_bars)

    with looking_up:
        if prefer_explicit:
            found = await asyncio.gather(*map(look_up_only, pending))
            pending = filter_prefer_explicit(
                [item for item, ok in zip(pending, found, strict=True) if ok]
            )
            progress.overall_add(len(pending), progress_bars)
            await asyncio.gather(*map(rip, pending))
        else:
            progress.overall_add(len(pending), progress_bars)
            await asyncio.gather(*(rip(item, looked_up=False) for item in pending))
