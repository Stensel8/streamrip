"""`streamrip plan`: what a download would do, said before it does anything."""

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


def show_plan(main: Main):
    """Print what is about to be downloaded, without downloading it."""
    if not main.media:
        console.print("Nothing to download: not found, or already downloaded.")
        return
    for item in main.media:
        if line := _line(item):
            console.print(f"  {line}", markup=False, highlight=False)
    planned = planned_tracks(main.media)
    pace = main.config.session.downloads.pacing
    console.print()
    for client, count in planned.items():
        client.pace(pace, count)
        console.print(f"{_account(client, count)}", markup=False, highlight=False)
    console.print("Nothing was downloaded.")


def _account(client: Client, count: int) -> str:
    """What one account would do: its tracks, the time at its pace and its hour."""
    load = client.load()
    seconds = client.time_for(count)
    how = (
        "at full speed"
        if seconds is None
        else f"{_minutes(seconds)} at its {load.tier} pace"
    )
    return (
        f"{client.source.capitalize()}: {plural(count, 'track')}, {how}; "
        f"{plural(load.this_hour, 'track')} started this hour"
    )
