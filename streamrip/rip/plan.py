"""`--preview`: what a download would do, said before it does anything."""

from ..console import console, plural
from ..media import Album, Artist, Label, Playlist, Track
from ..metadata.util import format_quality
from .main import ALBUM_TRACKS, Main, planned_tracks
lazy from ..client import Client


def _minutes(seconds: float) -> str:
    return f"about {max(round(seconds / 60), 1)} min"


def _line(item) -> str | None:
    """One resolved item as a line: its name, quality and how many tracks."""
    match item:
        case Album():
            info = item.meta.info
            quality = format_quality(info.container, info.bit_depth, info.sampling_rate)
            skipped = (
                f", {item.skipped_tracks} already downloaded"
                if item.skipped_tracks
                else ""
            )
            return f"{item.meta.album} {quality}: {plural(len(item.tracks), 'track')}{skipped}"
        case Playlist():
            return f"{item.name}: {plural(len(item.tracks), 'track')}"
        case Artist() | Label():
            return f"{plural(len(item.albums), 'album')}: about {len(item.albums) * ALBUM_TRACKS} tracks"
        case Track():
            return "1 track"
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
    already = f" ({load.this_hour} already this hour)" if load.this_hour else ""
    return f"{client.source.capitalize()}: {plural(count, 'track')}, {how}{already}"
