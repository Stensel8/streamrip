"""The interactive search menu's preview: a result's details, beside its cover."""

import io
import re
import shutil
import textwrap
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image
from rich.color import Color
from rich.console import Console
from rich.style import Style
from rich.text import Text

from ..console import console
from ..metadata import SearchResults, Summary

# Share of the terminal's height the menu gives the preview.
PREVIEW_SIZE = 0.5
# Columns the details get beside the cover, at least.
DETAILS_WIDTH = 60


def _fetch(url: str) -> bytes:
    """Download a cover; raises if it can't be had."""
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    return resp.content


def cover_size(columns: int, lines: int) -> int:
    """How many rows of the preview the cover gets: all of them (less the
    border), as long as that leaves the details enough columns. A cover is
    twice as many columns wide as it is rows tall.
    """
    return max(
        0, min(int(lines * PREVIEW_SIZE) - 2, (columns - DETAILS_WIDTH) // 2, 30)
    )


# Quadrant blocks by which quarters of the cell they fill: top left 1, top
# right 2, bottom left 4, bottom right 8.
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"
CORNERS = ((0, 0), (1, 0), (0, 1), (1, 1))


def _mean(pixels: list) -> tuple[int, int, int]:
    """The average color of some (r, g, b) pixels."""
    return tuple(sum(p[i] for p in pixels) // len(pixels) for i in range(3))


def _cell(pixels: list) -> tuple[str, tuple, tuple]:
    """The quadrant block, and its two colors, closest to a cell's 2x2 pixels."""
    best = None
    for mask in range(1, 8):  # the other 8 are these with the colors swapped
        on = [p for i, p in enumerate(pixels) if mask >> i & 1]
        off = [p for i, p in enumerate(pixels) if not mask >> i & 1]
        fg, bg = _mean(on), _mean(off)
        error = sum(
            sum((a - b) ** 2 for a, b in zip(p, fg if mask >> i & 1 else bg))
            for i, p in enumerate(pixels)
        )
        if best is None or error < best[0]:
            best = (error, QUADRANTS[mask], fg, bg)
    return best[1:]


def cover_rows(data: bytes, rows: int) -> list[Text]:
    """An image as rows of quadrant blocks, two colors per character: twice
    as sharp across as half blocks, which show only a top and bottom pixel.
    """
    columns = rows * 2  # a character is about twice as tall as it is wide
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image = image.resize((columns * 2, rows * 2), Image.Resampling.LANCZOS)
    lines = []
    for y in range(rows):
        line = Text()
        for x in range(columns):
            pixels = [image.getpixel((2 * x + dx, 2 * y + dy)) for dx, dy in CORNERS]
            char, fg, bg = _cell(pixels)
            line.append(
                char, Style(color=Color.from_rgb(*fg), bgcolor=Color.from_rgb(*bg))
            )
        lines.append(line)
    return lines


def detail_rows(summary: Summary, width: int) -> list[Text]:
    """The result's name, artist, details and description, one row each."""
    rows = [Text(summary.name, style="bold")]
    if summary.artist:
        rows.append(Text(summary.artist, style="cyan"))
    rows.append(Text())
    details = [*summary.details, ("ID", summary.id)]
    pad = max(len(label) for label, _ in details) + 2
    rows += [
        Text.assemble((label.ljust(pad), "dim"), value) for label, value in details
    ]
    if summary.explicit:
        rows.append(Text("Explicit", style="red"))
    if summary.description:
        rows.append(Text())
        rows += map(Text, textwrap.wrap(summary.description, max(width, 20)))
    return rows


class Previews:
    """The menu's preview command.

    Covers are fetched in the background from the moment the menu opens: a
    preview that waited for its cover would hold up every cursor move.
    """

    def __init__(self, results: SearchResults):
        """Start downloading every result's cover in the background."""
        self.results = results.results
        # In the menu's order: the first preview is drawn as the menu opens.
        urls = dict.fromkeys(r.image_url for r in self.results if r.image_url)
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._covers = {url: self._pool.submit(_fetch, url) for url in urls}
        # Renders with whatever colors this terminal supports.
        self._ansi = Console(
            file=io.StringIO(),
            force_terminal=True,
            color_system=console.color_system or "standard",
            width=1000,
        )

    def close(self):
        """Stop downloading covers."""
        self._pool.shutdown(wait=False, cancel_futures=True)

    def __call__(self, entry: str) -> str:
        """The preview of one menu entry: its cover beside its details."""
        match = re.match(r"\d+", entry)
        assert match is not None
        summary = self.results[int(match.group()) - 1]
        columns, lines = shutil.get_terminal_size()

        cover = self._cover(summary.image_url, cover_size(columns, lines))
        indent = len(cover[0]) + 2 if cover else 0
        # The preview's border takes four columns.
        details = detail_rows(summary, columns - 4 - indent)
        out = [
            Text.assemble(
                cover[i] if i < len(cover) else " " * (indent - 2),
                "  " if cover else "",
                details[i] if i < len(details) else "",
            )
            for i in range(max(len(cover), len(details)))
        ]

        with self._ansi.capture() as capture:
            for line in out:
                self._ansi.print(line, no_wrap=True, crop=False, soft_wrap=True)
        return capture.get().rstrip("\n")

    def _cover(self, url: str | None, rows: int) -> list[Text]:
        """The cover's rows, or none if it isn't there (yet)."""
        future = self._covers.get(url) if url and rows >= 4 else None
        try:
            return cover_rows(future.result(timeout=1), rows) if future else []
        except Exception:
            return []
