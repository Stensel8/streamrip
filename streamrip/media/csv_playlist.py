"""A list of tracks in a CSV file, found by searching a source.

Music-Sync (https://github.com/Stensel8/Music-Sync) exports a Spotify or Tidal
playlist as a CSV, and so do many other tools (Exportify, TuneMyMusic, ...). Each
row is searched on the source the user picks, the result that is the track (see
`audio_match`) is kept, and the tracks go in a folder named after the file, the
way a last.fm playlist does.
"""

import asyncio
import csv
import io
import logging
import re
from contextlib import nullcontext
from dataclasses import dataclass

from rich.text import Text

from ..client import Client
from ..client.audio_match import MatchTrack, rank, score, simplify_title
from ..config import Config
from ..console import console
from ..db import Database
from ..metadata import SearchResults, Summary
from .media import Pending
from .playlist import PendingPlaylistTrack, Playlist, _playlist_folder

logger = logging.getLogger("streamrip")

# From this score on a search result is the track (the threshold of Music-Sync).
MIN_SCORE = 0.8
# How many results of a search are looked at.
SEARCH_LIMIT = 5

# Every header spelling understood, by what it means: the columns of Music-Sync,
# and those of Exportify, TuneMyMusic, Soundiiz and the like.
_HEADERS = {
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


def _clock_ms(text: str | None) -> int | None:
    """ "3:45" or "1:02:03" in milliseconds, or None for anything else.

    A bare number could be seconds or milliseconds, so it says nothing.
    """
    if not text or not re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        return None
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def _duration_ms(record: dict[str, str]) -> int | None:
    if milliseconds := record.get("duration_ms"):
        try:
            return int(float(milliseconds))
        except ValueError:
            return None
    return _clock_ms(record.get("length"))


def _track(record: dict[str, str]) -> MatchTrack | None:
    """The track a row describes, or None if it has no title."""
    if not (title := record.get("title")):
        return None
    # Several artists are separated by ";" (a comma can be part of a name).
    artists = [a.strip() for a in record.get("artists", "").split(";") if a.strip()]
    return MatchTrack(title, artists, record.get("album", ""), _duration_ms(record))


def read_tracks(path: str) -> list[MatchTrack]:
    """The tracks in a CSV file. Rows without a title are skipped.

    The columns are found by their names. A file without a header row is read as
    "artist,title". The separator may be a comma, a semicolon (what Excel makes
    in many countries) or a tab. Raises ValueError for a file that is not UTF-8.
    """
    try:
        # utf-8-sig: Excel puts a byte order mark in front of it.
        with open(path, encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except UnicodeDecodeError as e:
        raise ValueError(
            f"{path} is not UTF-8 encoded. Save it as UTF-8 (in Excel: CSV UTF-8)."
        ) from e
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max(",;\t", key=first_line.count)  # a comma when there is a tie
    rows = [
        row
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
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
        logger.warning(f"{path}: skipped {skipped} row(s) without a title")
    return tracks


def _as_track(result: Summary) -> MatchTrack:
    """A search result as a track to compare the wanted one with."""
    details = dict(result.details)
    return MatchTrack(
        result.name,
        [result.artist] if result.artist else [],
        details.get("Album", ""),
        _clock_ms(details.get("Length")),
    )


def best_result(
    wanted: MatchTrack, results: list[Summary]
) -> tuple[Summary, float] | None:
    """The search result that is `wanted` and its score, if one is good enough."""
    candidates = [(result, _as_track(result)) for result in results]
    best = max(candidates, key=lambda c: rank(wanted, c[1]), default=None)
    if best is None or (found := score(wanted, best[1])) < MIN_SCORE:
        return None
    return best[0], found


def queries(wanted: MatchTrack) -> list[str]:
    """What to search for: the title without brackets and "feat." first, which is
    how a catalogue most often has it, then the title as the list has it.
    """
    titles = dict.fromkeys([simplify_title(wanted.title), wanted.title])
    return [f"{wanted.artist} {title}".strip() for title in titles]


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
        try:
            folder = _playlist_folder(self.config, self.name)
        except Exception as e:
            logger.error(f"Error creating playlist: {e}")
            return None

        found = failed = 0

        def status() -> Text:
            return Text.assemble(
                f"Searching {self.client.source.title()} for the tracks (",
                (f"{found} found", "bold green"),
                ", ",
                (f"{failed} not found", "bold red"),
                ", ",
                (f"{len(self.tracks)} total", "bold"),
                ")",
            )

        show = self.config.session.cli.progress_bars
        with (
            console.status(status(), spinner="moon") if show else nullcontext() as spin
        ):

            async def find(wanted: MatchTrack):
                """The (client, result) for one track, or None; keeps the count."""
                nonlocal found, failed
                hit = await self._find(wanted)
                if hit is None:
                    failed += 1
                    logger.warning(f"Not found: {wanted}")
                else:
                    found += 1
                if spin is not None:
                    spin.update(status())
                return hit

            hits = await asyncio.gather(*(find(t) for t in self.tracks))

        pending: list[PendingPlaylistTrack] = []
        seen: set[tuple[str, str]] = set()
        for position, hit in enumerate(hits, start=1):
            if hit is None:
                continue
            client, result = hit
            # A track listed twice must not be downloaded twice at the same time.
            if (client.source, result.id) in seen:
                logger.info(
                    f"Listed twice, downloaded once: {self.tracks[position - 1]}"
                )
                continue
            seen.add((client.source, result.id))
            pending.append(
                PendingPlaylistTrack(
                    result.id,
                    client,
                    self.config,
                    folder,
                    self.name,
                    position,
                    self.db,
                    total=len(self.tracks),
                )
            )
        logger.info(f"{self.name}: found {found} of {len(self.tracks)} tracks")
        if not pending:
            logger.error(f"None of the tracks of {self.name} was found")
            return None
        return Playlist(self.name, self.config, self.client, pending)

    async def _find(self, wanted: MatchTrack) -> tuple[Client, Summary] | None:
        """Search the source, then the fallback, for a track."""
        for client in (self.client, self.fallback_client):
            if client is None:
                continue
            for query in queries(wanted):
                try:
                    pages = await client.search("track", query, limit=SEARCH_LIMIT)
                    results = SearchResults.from_pages(client.source, "track", pages)
                except Exception as e:
                    # One failed search is no reason to give up the list.
                    logger.warning(
                        f"Searching {client.source} for {query!r} failed: {e}"
                    )
                    continue
                if (hit := best_result(wanted, results.results)) is not None:
                    logger.debug(
                        f"{wanted} is {hit[0].name!r} on {client.source} "
                        f"(score {hit[1]:.2f}, searched for {query!r})"
                    )
                    return client, hit[0]
        return None
