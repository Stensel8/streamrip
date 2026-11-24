"""A transfer's audit trail: what was wanted, what was found, and how sure."""

import csv
lazy from collections.abc import Iterable
lazy from pathlib import Path

lazy from .models import Match, Track

ARTISTS = "; "
COLUMNS = (
    "result",
    "how",
    "score",
    "wanted title",
    "wanted artists",
    "wanted isrc",
    "wanted seconds",
    "found title",
    "found artists",
    "found isrc",
    "found seconds",
    "seconds apart",
    "why",
)


def _seconds(track: Track | None) -> str:
    return (
        ""
        if track is None or track.duration_ms is None
        else str(round(track.duration_ms / 1000))
    )


def _row(
    result: str,
    wanted: Track | None,
    found: Match | None,
    why: str = "",
) -> list[str]:
    candidate = None if found is None else found.track
    apart = ""
    if (
        wanted
        and candidate
        and wanted.duration_ms is not None
        and candidate.duration_ms is not None
    ):
        apart = str(round(abs(wanted.duration_ms - candidate.duration_ms) / 1000))
    return [
        result,
        "" if found is None else found.method,
        "" if found is None else f"{found.score:.2f}",
        wanted.title if wanted else "",
        ARTISTS.join(wanted.artists) if wanted else "",
        (wanted.isrc or "") if wanted else "",
        _seconds(wanted),
        candidate.title if candidate else "",
        ARTISTS.join(candidate.artists) if candidate else "",
        (candidate.isrc or "") if candidate else "",
        _seconds(candidate),
        apart,
        why,
    ]


def report_csv(fh, matched: Iterable[Match], misses: Iterable) -> int:
    """Write a row for every track wanted: the match it got, or why it got none
    (with the closest candidate, if there was one). Returns the rows written."""
    rows = [_row("matched", m.wanted, m) for m in matched]
    rows += [_row("not found", miss.track, miss.closest, miss.why) for miss in misses]
    writer = csv.writer(fh)
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return len(rows)


def write_report(path: Path, matched: Iterable[Match], misses: Iterable) -> int:
    """`report_csv` into the file at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        return report_csv(fh, matched, misses)
