"""Playlists as M3U8 files, pointing at the files of a music library on disk.

A player or server (Navidrome, Jellyfin, Music Assistant, ...) reads an M3U8
playlist, but only if its entries are files that exist. Spotify and Tidal do
not know where those are, so the library is scanned: each track of a playlist
is found by its ISRC, which ripping tools never change, or else by title,
artist and length, as the rest of streamrip matches tracks.
"""

import logging
import os
from dataclasses import dataclass, field
lazy from collections.abc import Callable, Iterable
lazy from pathlib import Path

import mutagen
from mutagen.easymp4 import EasyMP4Tags

from ..client.audio_match import MatchTrack, best_match, normalize, normalize_isrc

logger = logging.getLogger("streamrip")

AUDIO_EXTENSIONS = frozenset(
    {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".aiff", ".aif", ".wav"}
)
# MP4 keeps the ISRC in a freeform atom, as streamrip's tagger writes it; the
# easy interface reads it once it knows the name.
EasyMP4Tags.RegisterFreeformKey("isrc", "ISRC")


def _first(tags, key: str) -> str:
    values = tags.get(key) if tags is not None else None
    return str(values[0]).strip() if values else ""


def read_file(path: Path) -> MatchTrack | None:
    """The track an audio file holds, by its tags; None for one without a
    title or that cannot be read. Its path is in ids["file"], its date
    (for choosing between releases) in ids["date"]."""
    try:
        audio = mutagen.File(path, easy=True)
    except mutagen.MutagenError, OSError:
        return None
    if audio is None or not (title := _first(audio.tags, "title")):
        return None
    seconds = getattr(audio.info, "length", None)
    return MatchTrack(
        title,
        [a.strip() for a in (audio.tags.get("artist") or []) if str(a).strip()],
        _first(audio.tags, "album"),
        round(seconds * 1000) if seconds else None,
        isrc=normalize_isrc(_first(audio.tags, "isrc")),
        ids={"file": str(path), "date": _first(audio.tags, "date")},
    )


@dataclass(slots=True)
class Library:
    """The audio files under a folder, by ISRC and by title. Not by artist: a tag
    can hold "A feat. B" or "A/B" where the service lists A and B."""

    root: Path
    by_isrc: dict[str, list[MatchTrack]] = field(default_factory=dict)
    by_title: dict[str, list[MatchTrack]] = field(default_factory=dict)
    size: int = 0

    @classmethod
    def scan(cls, root: Path, on_file: Callable[[int], None] | None = None) -> Library:
        """Read the tags of every audio file under `root`."""
        library = cls(root)
        for path in sorted(
            root.rglob("*", recurse_symlinks=True)
        ):  # a linked artist folder too
            if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
                continue
            if (track := read_file(path)) is None:
                continue
            library.size += 1
            if track.isrc:
                library.by_isrc.setdefault(track.isrc, []).append(track)
            library.by_title.setdefault(normalize(track.title), []).append(track)
            if on_file:
                on_file(library.size)
        return library

    def find(self, track: MatchTrack, min_score: float = 0.8) -> MatchTrack | None:
        """The file for `track`: by ISRC, else by title, artist and length."""
        if track.isrc and (found := self.by_isrc.get(track.isrc.upper())):
            # One recording can be on several releases: the oldest one, then
            # the first path, so a playlist comes out the same every time.
            chosen = min(found, key=lambda t: (t.ids["date"] or "9999", t.ids["file"]))
            if len(found) > 1:
                logger.debug(
                    "%s is in the library %s times; took %s",
                    track,
                    len(found),
                    chosen.ids["file"],
                )
            return chosen
        hit = best_match(
            track, self.by_title.get(normalize(track.title), []), min_score
        )
        return hit[0] if hit else None


def _extinf(track: MatchTrack) -> str:
    seconds = round(track.duration_ms / 1000) if track.duration_ms else -1
    return f"#EXTINF:{seconds},{track}"


def write_m3u8(
    path: Path,
    tracks: Iterable[MatchTrack],
    library: Library | None = None,
    *,
    absolute: bool = False,
    min_score: float = 0.8,
) -> tuple[int, list[MatchTrack]]:
    """Write `tracks` as an extended M3U playlist (UTF-8) at `path`.

    With a library, each entry is the path of the matching file, relative to
    the playlist unless `absolute`; tracks it does not have are left out.
    Without one, the entries only describe the tracks (#EXTINF and #EXTALB),
    so the file still documents the playlist. Returns how many entries were
    written, and the tracks left out.
    """
    lines, missing = ["#EXTM3U"], []
    for track in tracks:
        if library is None:
            lines.append(_extinf(track))
            if track.album:
                lines.append(f"#EXTALB:{track.album}")
            continue
        if (file := library.find(track, min_score)) is None:
            missing.append(track)
            continue
        target = os.path.abspath(file.ids["file"])  # noqa: PTH100
        try:
            # The two folders where they really are (either may be reached
            # through a link), the file by its place in the library, not where
            # a link in it leads: a player may see only the library.
            entry = (
                target
                if absolute
                else os.path.relpath(
                    library.root.resolve()
                    / Path(target).relative_to(library.root.absolute()),
                    path.parent.resolve(),
                )
            )
        except ValueError:  # another drive than the playlist's, on Windows
            entry = str(target)
        lines += [_extinf(file), entry.replace(os.sep, "/")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written = sum(1 for line in lines if line.startswith("#EXTINF"))
    return written, missing
