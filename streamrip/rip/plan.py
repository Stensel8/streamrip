"""`--preview`: what a download would do, said before it does anything."""

from ..console import console, plural
from ..media import Album, Artist, Label, Playlist, Track
from ..metadata.util import format_quality
from .main import ALBUM_TRACKS, Main, planned_tracks
lazy from ..client import Client


def _minutes(seconds: float) -> str:
    return f"about {max(round(seconds / 60), 1)} min"


def _line(item) -> str | None:
    """One resolved item as a line: what it is and its name, its quality, and
    its tracks."""
    match item:
        case Album():
            meta, info = item.meta, item.meta.info
            quality = format_quality(info.container, info.bit_depth, info.sampling_rate)
            tracks = plural(len(item.tracks), "track")
            before = (
                f" ({item.skipped_tracks} downloaded before)"
                if item.skipped_tracks
                else ""
            )
            return (
                f"Album: {meta.albumartist} - {meta.album} {quality}, {tracks}{before}"
            )
        case Track():
            meta, info = item.meta, item.meta.album.info
            quality = format_quality(info.container, info.bit_depth, info.sampling_rate)
            return f"Track: {meta.artist} - {meta.title} {quality}"
        case Playlist():
            return f"Playlist: {item.name}, {plural(len(item.tracks), 'track')}"
        case Artist() | Label():
            albums = plural(len(item.albums), "album")
            about = len(item.albums) * ALBUM_TRACKS
            return (
                f"{type(item).__name__}: {item.name}, {albums} (about {about} tracks)"
            )
    return None


def plan_lines(main: Main) -> tuple[list[str], list[str], str]:
    """What is about to be downloaded, per item and per account, without
    downloading it; and all of it in one sentence."""
    items = [line for item in main.media if (line := _line(item))]
    pace = main.config.session.downloads.pacing
    planned = planned_tracks(main.media)
    accounts = []
    for client, count in planned.items():
        client.pace(pace, count, say=False)
        accounts.append(_account(client, count))
    sources = [f"{count} from {c.source.capitalize()}" for c, count in planned.items()]
    total = plural(sum(planned.values()), "track")
    if len(sources) == 1:
        summary = f"{total} would be downloaded {sources[0].partition(' ')[2]}."
    else:
        summary = f"{total} would be downloaded: {', '.join(sources)}."
    return items, accounts, summary


def show_plan(main: Main):
    """Print the plan."""
    if not main.media:
        console.print("Nothing to download: not found, or already downloaded.")
        return
    items, accounts, summary = plan_lines(main)
    for line in items:
        console.print(f"  {line}", markup=False, highlight=False)
    console.print()
    for line in accounts:
        console.print(line, markup=False, highlight=False)
    console.print(f"{summary} Nothing was downloaded.", markup=False, highlight=False)


def _account(client: Client, count: int) -> str:
    """What one account would do: its tracks, the time at its pace and its hour."""
    if not client.paced:
        return f"{client.source.capitalize()}: {plural(count, 'track')}"
    load = client.load()
    seconds = client.time_for(count)
    how = (
        "at full speed"
        if seconds is None
        else f"{_minutes(seconds)} at its {load.tier} pace"
    )
    earlier = f" ({load.this_hour} earlier this hour)" if load.this_hour else ""
    return f"{client.source.capitalize()}: {plural(count, 'track')}, {how}{earlier}"
