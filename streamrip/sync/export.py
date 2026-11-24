"""Saving liked songs and playlists as CSV or M3U8 files, for `streamrip
playlist export` and the web interface."""

from itertools import count
from pathlib import Path
lazy from collections.abc import Callable

from ..console import plural
from .csvio import slug, write_tracks
from .m3u import Library, write_m3u8
lazy from .models import Track


def free_name(name: str, taken: set[str]) -> str:
    """`name`, or "name (2)" and so on when an earlier file took it. Two
    playlists can share a name, and `slug` makes more names alike; "Mix" and
    "mix" are one file on Windows and macOS."""
    free = next(
        n
        for i in count(1)
        if (n := name if i == 1 else f"{name} ({i})").casefold() not in taken
    )
    taken.add(free.casefold())
    return free


def export_lists(
    selected: list[tuple[str, list[Track]]],
    *,
    everything: bool,
    output: Path | None,
    service: str,
    m3u8: bool = False,
    library: Path | None = None,
    absolute: bool = False,
    say: Callable[[str], None] = lambda _line: None,
) -> tuple[list[str], list[Track]]:
    """Write the lists, one file each in a folder (`everything`) or one file.

    Returns a line per file, and for M3U8 the tracks the music folder lacks.
    `say` hears what takes time: reading the tags of the music folder.
    """
    music = None
    if m3u8 and library:
        say(f"Reading the tags of the music in {library}")
        music = Library.scan(library)
        say(f"{plural(music.size, 'audio file')} in {library}")
    elif m3u8:
        say(
            "No library: the playlists only describe the tracks, without files a "
            "player can play."
        )
    missing: list[Track] = []

    def write(path: Path, tracks: list[Track]) -> str:
        if not m3u8:
            return f"{write_tracks(path, tracks)} tracks"
        written, left_out = write_m3u8(path, tracks, music, absolute=absolute)
        missing.extend(left_out)
        return f"{written} tracks" + (
            f", {len(left_out)} not in the library" if left_out else ""
        )

    suffix = ".m3u8" if m3u8 else ".csv"
    lines = []
    if everything:
        folder = output or Path("streamrip-export") / service
        taken: set[str] = set()
        for name, tracks in selected:
            path = folder / f"{free_name(slug(name), taken)}{suffix}"
            lines.append(f"{write(path, tracks):>5} -> {path}")
    else:
        ((name, tracks),) = selected
        path = output or Path(f"{slug(name)}{suffix}")
        lines.append(f"{write(path, tracks)} -> {path}")
    return lines, missing
