"""Retrying the downloads that failed, for `streamrip repair` and the web interface."""

import logging

from .. import db
from ..console import console, plural
from ..media import Album
from .main import Main
lazy from ..config import Config

logger = logging.getLogger("streamrip")


async def _albums_for(main, failed_items):
    """Map failed tracks onto the albums that contain them.

    Returns (album targets, items to retry as they are). Anything whose album
    cannot be determined is passed through untouched rather than dropped.
    """
    targets: list[tuple[str, str, str]] = []
    unresolved: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for source, media_type, item_id in failed_items:
        if media_type != "track":
            targets.append((source, media_type, item_id))
            continue
        try:
            client = await main.get_logged_in_client(source)
            resp = await client.get_metadata(item_id, "track")
            album_id = str((resp.get("album") or {}).get("id") or "")
        except Exception as e:
            logger.debug("Could not find the album for %s: %s", item_id, e)
            album_id = ""

        if not album_id:
            unresolved.append((source, media_type, item_id))
            continue
        if (source, album_id) not in seen:
            seen.add((source, album_id))
            targets.append((source, "album", album_id))

    return targets, unresolved


async def repair_downloads(
    config: Config, flat: bool = False, *, interactive: bool = True
) -> int:
    """Retry every item of the failed downloads database; what worked is
    forgotten there. Returns how many were repaired."""
    singles = config.session.filepaths.add_singles_to_folder
    recording = config.session.database.downloads_enabled
    # A repaired track is nearly always a track missing from an album that was
    # otherwise downloaded, so it needs to land in that album's folder rather
    # than loose in the download root. This only touches the in-memory session
    # copy, so config.toml is left alone.
    if not flat:
        config.session.filepaths.add_singles_to_folder = True
    # What succeeded is read back from the downloads db below, so it is kept
    # even with --no-db or downloads_enabled = false.
    config.session.database.downloads_enabled = True
    try:
        return await _repair(config, interactive)
    finally:
        # A server goes on with other downloads after this one.
        config.session.filepaths.add_singles_to_folder = singles
        config.session.database.downloads_enabled = recording


async def _repair(config: Config, interactive: bool) -> int:
    """Retry the failed items; how many were repaired."""
    database = db.Database(
        db.Downloads(config.session.database.downloads_path),
        db.Failed(config.session.database.failed_downloads_path),
    )
    failed_items = database.failed.all()
    if not failed_items:
        return 0

    # A failed item should never also be logged as downloaded, but older
    # versions of streamrip could mark one downloaded even after it failed.
    # Clear that stale state so the retry below isn't skipped.
    for source, _media_type, item_id in failed_items:
        database.forget_downloaded(source, item_id)

    async with Main(config, interactive=interactive) as main:
        # Retry through the album rather than track by track. Resolving a
        # single track builds its album metadata from the track response, which
        # on Tidal carries only an id, title and cover -- no track count, and
        # the track's artists in place of the album artist. The folder that
        # produces differs from the album's own in both, so repaired tracks
        # land in a separate folder instead of rejoining the album.
        #
        # Going through the album gets the real metadata, the right folder,
        # disc subfolders and cover art, and costs nothing extra: tracks
        # already in the downloads db are skipped, so only what is missing is
        # fetched.
        targets, unresolved = await _albums_for(main, failed_items)
        # An album is the way to a failed track (its tags, its folder), not a
        # reason to download the rest of it: only the tracks that failed.
        failed_tracks = {(s, i) for s, kind, i in failed_items if kind == "track"}
        if unresolved:
            console.print(
                f"[yellow]{plural(len(unresolved), 'item')} had no album to retry "
                "through; fetching them individually."
            )
        await main.add_all_by_id(targets + unresolved)
        await main.resolve()
        for item in main.media:
            if isinstance(item, Album) and item.client is not None:
                source = item.client.source
                # Ids as the failures table keeps them: text (an album lists numbers).
                retry = [t for t in item.tracks if (source, str(t.id)) in failed_tracks]
                item.tracks = retry or item.tracks  # or the album failed as a whole
        await main.rip()

    # Nothing in the download pipeline removes rows from the failed db, so
    # success can't be detected by diffing it. But set_downloaded() is only
    # reached via postprocess(), which a failed download never gets to, so an
    # item in the downloads db now is one that just succeeded.
    repaired = [
        (source, media_type, item_id)
        for source, media_type, item_id in failed_items
        if database.downloaded(source, item_id)
    ]
    for source, media_type, item_id in repaired:
        database.forget_failed(source, media_type, item_id)
    return len(repaired)
