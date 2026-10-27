"""A list of tracks in a CSV file, found by searching a source.

Music-Sync (https://github.com/Stensel8/Music-Sync) exports a Spotify or Tidal
playlist as a CSV, and so do many other tools (Exportify, TuneMyMusic, ...). This
reads the file; each row is then searched for on the source the user picks, the
way a last.fm playlist is (see `playlist.searched_playlist`).
"""

import csv
import io
import logging
from dataclasses import dataclass
from itertools import islice
lazy from pathlib import Path

from ..client.audio_match import MatchTrack
from .media import Pending
from .playlist import Playlist, clock_ms, searched_playlist
lazy from ..client import Client
lazy from ..config import Config
lazy from ..db import Database

logger = logging.getLogger("streamrip")

# How many rows of a file are looked at to find out what separates its cells.
SAMPLE_ROWS = 10

# Every header spelling understood, by what it means: the columns of Music-Sync,
# and those of Exportify, TuneMyMusic, Soundiiz and the like.
_HEADERS = frozendict(
    {
        alias: field
        for field, aliases in {
            "title": (
                "title",
                "track name",
                "track title",
                "track",
                "name",
                "song",
                "song name",
            ),
            "artists": (
                "artists",
                "artist",
                "artist name(s)",
                "artist names",
                "artist name",
            ),
            "album": ("album", "album name"),
            "duration_ms": ("duration_ms", "duration (ms)"),
            "length": ("duration", "length", "time"),
        }.items()
        for alias in aliases
    }
)


def _duration_ms(record: dict[str, str]) -> int | None:
    if milliseconds := record.get("duration_ms"):
        try:
            return int(float(milliseconds))
        except ValueError:
            return None
    return clock_ms(record.get("length"))


def _track(record: dict[str, str]) -> MatchTrack | None:
    """The track a row describes, or None if it has no title."""
    if not (title := record.get("title")):
        return None
    # Several artists are separated by ";" (a comma can be part of a name).
    artists = [a.strip() for a in record.get("artists", "").split(";") if a.strip()]
    return MatchTrack(title, artists, record.get("album", ""), _duration_ms(record))


def _rows(text: str, delimiter: str):
    """The rows of a CSV text that have something in them."""
    # newline="": a line may end in \r alone (Excel for Mac's "CSV (Macintosh)").
    for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter):
        if any(cell.strip() for cell in row):
            yield row


def _delimiter(text: str) -> str:
    """What separates the cells of a CSV text: a comma, a semicolon or a tab.

    It is the one that cuts the first rows into the same number of cells. Counting
    them in the first line does not tell: a name can hold one ("Earth, Wind & Fire"
    in a file of semicolons, quoted or not). A comma when nothing tells them apart.
    """
    best, best_key = ",", None
    for delimiter in ",;\t":
        widths = [len(row) for row in islice(_rows(text, delimiter), SAMPLE_ROWS)]
        if max(widths, default=0) < 2:
            continue  # it separates nothing
        # Rows of one width first, and then as many cells as the header has.
        key = (len(set(widths)) == 1, widths[0])
        if best_key is None or key > best_key:
            best, best_key = delimiter, key
    return best


def read_tracks(path: Path) -> list[MatchTrack]:
    """The tracks in a CSV file. Rows without a title are skipped.

    The columns are found by their names. A file without a header row is read as
    "artist,title". The separator may be a comma, a semicolon (what Excel makes
    in many countries) or a tab. Raises ValueError for a file that is not UTF-8.
    """
    try:
        # utf-8-sig: Excel puts a byte order mark in front of it.
        text = path.read_text(encoding="utf-8-sig", newline="")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"{path} is not UTF-8 encoded. Save it as UTF-8 (in Excel: CSV UTF-8)."
        ) from e
    rows = list(_rows(text, _delimiter(text)))
    if not rows:
        return []

    header = [_HEADERS.get(cell.strip().lower()) for cell in rows[0]]
    if "title" in header:
        columns, body = header, rows[1:]
    else:
        columns, body = ["artists", "title"], rows  # no header row: artist,title
    tracks = []
    for row in body:
        record = {c: v.strip() for c, v in zip(columns, row, strict=False) if c}
        if (track := _track(record)) is not None:
            tracks.append(track)
    if skipped := len(body) - len(tracks):
        logger.warning("%s: skipped %s row(s) without a title", path, skipped)
    return tracks


@dataclass(slots=True)
class PendingCsvPlaylist(Pending):
    """A CSV list, to be searched on a source (and on another for what it lacks)."""

    name: str
    tracks: list[MatchTrack]
    client: Client
    fallback_client: Client | None
    config: Config
    db: Database

    async def resolve(self) -> Playlist | None:
        """Search every track and make a playlist of the ones that were found."""
        return await searched_playlist(
            self.name,
            self.tracks,
            self.client,
            self.fallback_client,
            self.config,
            self.db,
        )
