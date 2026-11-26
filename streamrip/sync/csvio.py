"""Track lists as CSV files: read for `streamrip csv` and `streamrip playlist
import`, written by `streamrip playlist export`.

Reading takes what streamrip itself, Exportify, TuneMyMusic, Soundiiz or a
spreadsheet make; writing keeps the columns it always had, so an older export
still reads the same.
"""

import csv
import io
import logging
import re
from itertools import islice
lazy from collections.abc import Iterable
lazy from pathlib import Path
lazy from typing import TextIO

from ..client.audio_match import MatchTrack, normalize_isrc
from ..console import plural
from .errors import CsvError

logger = logging.getLogger("streamrip")

COLUMNS = (
    "title",
    "artists",
    "album",
    "duration_ms",
    "isrc",
    "spotify_uri",
    "tidal_id",
)
ARTIST_SEPARATOR = "; "
# How many rows of a file are looked at to find out what separates its cells.
SAMPLE_ROWS = 10

# Every header spelling understood, by what it means: the columns written
# here, and those of Exportify, TuneMyMusic, Soundiiz and the like.
_HEADERS = frozendict(
    {
        alias: column
        for column, aliases in {
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
            "isrc": ("isrc",),
            "spotify_uri": ("spotify_uri", "track uri", "spotify uri", "uri"),
            "tidal_id": ("tidal_id", "tidal id"),
        }.items()
        for alias in aliases
    }
)


def clock_ms(text: str | None) -> int | None:
    """ "3:45" or "1:02:03" in milliseconds, or None for anything else.

    A bare number could be seconds or milliseconds, so it says nothing.
    """
    if not text or not re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        return None
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def slug(name: str) -> str:
    """A file name for a playlist name: keeps letters, digits, spaces and dashes."""
    return re.sub(r"[^\w\- ]+", "_", name).strip(" _") or "playlist"


def _duration_ms(record: dict[str, str]) -> int | None:
    if milliseconds := record.get("duration_ms"):
        try:
            return int(float(milliseconds))
        except ValueError, OverflowError:  # no number, or "inf"
            return None
    return clock_ms(record.get("length"))


def _track(record: dict[str, str]) -> MatchTrack | None:
    """The track a row describes, or None if it has no title. In a list of
    albums (an album column, no title column) the album is the title."""
    if not (title := record.get("title") or record.get("album")):
        return None
    ids = {}
    if record.get("spotify_uri", "").startswith("spotify:track:"):
        ids["spotify"] = record["spotify_uri"]
    if record.get("tidal_id"):
        ids["tidal"] = record["tidal_id"]
    return MatchTrack(
        title,
        # Several artists are separated by ";" (a comma can be part of a name).
        [a.strip() for a in record.get("artists", "").split(";") if a.strip()],
        record.get("album", ""),
        _duration_ms(record),
        isrc=normalize_isrc(record.get("isrc")),
        ids=ids,
    )


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


def parse_csv(text: str, source: str = "the CSV") -> list[MatchTrack]:
    """The tracks in the text of a CSV file. Rows without a title are skipped.

    The columns are found by their names. A file without a header row is read as
    "artist,title". The separator may be a comma, a semicolon (what Excel makes
    in many countries) or a tab.
    """
    # A file's name, which a browser sends: never a line of its own in the log.
    source = source.replace("\r", " ").replace("\n", " ")
    try:
        rows = list(_rows(text, _delimiter(text)))
    except csv.Error as e:  # a quote that is never closed, in a big file
        raise CsvError(f"{source} cannot be read as CSV: {e}") from e
    if not rows:
        return []

    header = [_HEADERS.get(cell.strip().lower()) for cell in rows[0]]
    if "title" in header or "album" in header:
        columns, body = header, rows[1:]
    else:
        columns, body = ["artists", "title"], rows  # no header row: artist,title
    tracks = []
    for row in body:
        # The first column of a kind counts: after "Title", a "Track" column
        # holds the track's number, which is not to replace the title.
        record: dict[str, str] = {}
        for column, value in zip(columns, row, strict=False):
            if column:
                record.setdefault(column, value.strip())
        if (track := _track(record)) is not None:
            tracks.append(track)
    if skipped := len(body) - len(tracks):
        logger.warning("%s: skipped %s without a title", source, plural(skipped, "row"))
    return tracks


def decode(data: bytes, source: str = "the CSV") -> str:
    """The text of a CSV file's bytes, which have to be UTF-8."""
    try:
        # utf-8-sig: Excel puts a byte order mark in front of it.
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CsvError(
            f"{source} is not UTF-8 encoded. Save it as UTF-8 (in Excel: CSV UTF-8)."
        ) from e


def read_tracks(path: Path) -> list[MatchTrack]:
    """The tracks in a CSV file (see parse_csv). Raises CsvError, a ValueError,
    for a file that is not UTF-8 or not CSV."""
    return parse_csv(decode(path.read_bytes(), str(path)), str(path))


def write_csv(fh: TextIO, tracks: Iterable[MatchTrack]) -> int:
    """Write `tracks` with a header row; returns how many were written."""
    writer = csv.writer(fh)
    writer.writerow(COLUMNS)
    rows = [
        (
            t.title,
            ARTIST_SEPARATOR.join(t.artists),
            t.album,
            "" if t.duration_ms is None else t.duration_ms,
            t.isrc or "",
            t.ids.get("spotify", ""),
            t.ids.get("tidal", ""),
        )
        for t in tracks
    ]
    writer.writerows(rows)
    return len(rows)


def write_tracks(path: Path, tracks: Iterable[MatchTrack]) -> int:
    """Write `tracks` to `path`, creating folders as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        return write_csv(fh, tracks)
