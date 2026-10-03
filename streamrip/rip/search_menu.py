"""Interactive search: results above a cover and its metadata, on every OS."""

import asyncio
import textwrap
from collections import OrderedDict
from typing import ClassVar

import aiohttp
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..client import new_session
from ..metadata import SearchResults, Summary
from .cover_preview import CoverPane, decode_cover, image_widget

PREVIEW_SIZE = 0.5
DETAILS_WIDTH = 60
MAX_COVER_BYTES = 5 * 1024 * 1024


def cover_size(columns: int, lines: int) -> int:
    """Rows for a square cover, leaving room for the border and metadata."""
    return max(
        0, min(int(lines * PREVIEW_SIZE) - 2, (columns - 4 - DETAILS_WIDTH) // 2, 30)
    )


class CoverCache:
    """Fetch on demand so the active result never queues behind other covers.

    Completed images (including failures) are cached by URL for this session.
    A cancelled request is not cached, allowing a later selection to retry it.
    """

    def __init__(self, verify_ssl: bool = True):
        self.verify_ssl = verify_ssl
        self.session = None
        self.images = OrderedDict()

    async def _fetch(self, url: str) -> bytes:
        if self.session is None:
            self.session = new_session(
                self.verify_ssl, timeout=aiohttp.ClientTimeout(total=5)
            )
        async with self.session.get(url) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_COVER_BYTES:
                    raise ValueError("Cover too large for a preview")
            return bytes(data)

    async def get(self, urls: tuple[str, ...]):
        for url in urls:
            if url not in self.images:
                try:
                    data = await self._fetch(url)
                    self.images[url] = await asyncio.to_thread(decode_cover, data)
                except Exception:
                    self.images[url] = None
                if len(self.images) > 32:
                    self.images.popitem(last=False)
            self.images.move_to_end(url)
            if self.images[url] is not None:
                return self.images[url]
        return None

    async def close(self):
        if self.session is not None:
            await self.session.close()
        self.images.clear()


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


class SearchMenu(App[tuple[int, ...] | None]):
    """Selection indices have the same meaning as SearchResults.get_choices."""

    CSS = """
    Screen { background: ansi_default; color: ansi_default; }
    #heading { height: auto; padding: 0 1; }
    #help { height: 1; padding: 0 1; color: ansi_bright_black; }
    #filter { display: none; height: 3; }
    #results { height: 1fr; min-height: 3; border: none; padding: 0 1;
               background: ansi_default; }
    #preview { height: 50%; min-height: 3; border: round ansi_cyan; padding: 0 1; }
    #cover { margin-right: 2; }
    #details-scroll { width: 1fr; height: 1fr; }
    #details { width: 1fr; height: auto; }
    OptionList > .option-list--option-highlighted { background: ansi_blue; color: ansi_white; }
    """
    BINDINGS: ClassVar = [
        Binding("space", "toggle", "Select", priority=True),
        Binding("enter", "accept", "Download", priority=True),
        Binding("escape", "cancel", "Exit", priority=True),
        Binding("ctrl+c", "quit_search", "Exit", priority=True),
        Binding("slash", "filter", "Search"),
    ]

    def __init__(
        self,
        results: SearchResults,
        source: str,
        media_type: str,
        query: str,
        *,
        pixel_widget=None,
        verify_ssl: bool = True,
    ):
        super().__init__()
        self.results = results.results
        self.heading = f"Results for {media_type} '{query}' from {source.capitalize()}"
        self.preview_title = f"{source.capitalize()} {media_type}"
        self.pixel_widget = pixel_widget
        self.covers = CoverCache(verify_ssl)
        self.selected: set[int] = set()
        self.visible_indices = list(range(len(self.results)))
        self.current: int | None = None
        self._cover_urls: tuple[str, ...] = ()
        self._cover_worker = None
        self._preview_resize_timer = None
        self._resizing_preview = False

    def _option(self, index: int) -> Option:
        mark = "[x]" if index in self.selected else "[ ]"
        return Option(
            Text(
                f"{mark} {index + 1}. {self.results[index].summarize()}",
                no_wrap=True,
                overflow="ellipsis",
            ),
            id=str(index),
        )

    def compose(self) -> ComposeResult:
        yield Static(Text(self.heading), id="heading")
        yield Static(
            "SPACE - select, ENTER - download, / - search, ESC - exit", id="help"
        )
        yield Input(placeholder="Search results…", id="filter")
        yield OptionList(*(self._option(i) for i in self.visible_indices), id="results")
        with Horizontal(id="preview"):
            yield CoverPane(self.pixel_widget)
            with VerticalScroll(id="details-scroll"):
                yield Static(id="details", markup=False)

    def on_mount(self):
        self.query_one("#preview").border_title = self.preview_title
        self.query_one(OptionList).focus()
        self.call_after_refresh(self._resize_preview)

    def on_resize(self):
        # SIXEL drawn with the previous geometry can scroll the whole terminal
        # while it is shrinking. Hide it until Textual has laid out the new size.
        if not self.is_mounted:
            return
        self._resizing_preview = True
        self.query_one(CoverPane).display = False
        if self._preview_resize_timer is not None:
            self._preview_resize_timer.stop()
        self._preview_resize_timer = self.set_timer(0.1, self._finish_resize)

    def _finish_resize(self):
        self._resizing_preview = False
        self._resize_preview()
        self.refresh(layout=True)

    def _resize_preview(self):
        pane = self.query_one(CoverPane)
        rows = cover_size(self.size.width, self.size.height)
        pane.display = (
            not self._resizing_preview and rows >= 4 and bool(self._cover_urls)
        )
        pane.styles.width = rows * 2
        pane.styles.height = rows
        self._show_details()

    def _show_details(self):
        details = self.query_one("#details", Static)
        if self.current is None:
            details.update("No matching results")
        else:
            width = self.query_one("#details-scroll").size.width
            details.update(
                Text("\n").join(detail_rows(self.results[self.current], max(width, 20)))
            )

    @on(OptionList.OptionHighlighted)
    async def highlight(self, event: OptionList.OptionHighlighted):
        # Option ids remain stable while a filter changes the visible indices.
        index = int(event.option.id)
        if index not in self.visible_indices:
            return
        self.current = index
        self.query_one("#details-scroll").scroll_home(animate=False)
        self._show_details()
        summary = self.results[index]
        urls = summary.image_urls
        if urls != self._cover_urls:
            self._cover_urls = urls
            if self._cover_worker is not None:
                self._cover_worker.cancel()
            await self.query_one(CoverPane).show_image(None)
            if urls:
                self._cover_worker = self.run_worker(
                    self._load_cover(urls), group="cover", exclusive=True
                )
        self._resize_preview()

    async def _load_cover(self, urls: tuple[str, ...]):
        # Avoid fetching every intermediate result when holding an arrow key.
        await asyncio.sleep(0.08)
        image = await self.covers.get(urls)
        if urls == self._cover_urls:
            await self.query_one(CoverPane).show_image(image)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle" and self.query_one(Input).has_focus:
            return False  # A space belongs to the search query.
        return True

    def action_toggle(self):
        if self.current is None:
            return
        index = self.current
        if index in self.selected:
            self.selected.remove(index)
        else:
            self.selected.add(index)
        self.query_one(OptionList).replace_option_prompt(
            str(index), self._option(index).prompt
        )

    def action_accept(self):
        # The previous Linux menu also selected the highlighted result on
        # Enter, even when other results had already been marked with Space.
        choices = set(self.selected)
        if self.current is not None:
            choices.add(self.current)
        choices = tuple(sorted(choices))
        if choices:
            self.exit(choices)

    def action_filter(self):
        field = self.query_one(Input)
        field.display = True
        field.focus()

    @on(Input.Changed)
    async def filter_results(self, event: Input.Changed):
        query = event.value.casefold()
        previous = self.current
        self.visible_indices = [
            i for i, r in enumerate(self.results) if query in r.summarize().casefold()
        ]
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(self._option(i) for i in self.visible_indices)
        if self.visible_indices:
            options.highlighted = (
                self.visible_indices.index(previous)
                if previous in self.visible_indices
                else 0
            )
        else:
            self.current = None
            self._cover_urls = ()
            if self._cover_worker is not None:
                self._cover_worker.cancel()
            await self.query_one(CoverPane).show_image(None)
            self._resize_preview()

    def action_cancel(self):
        field = self.query_one(Input)
        if field.display:
            field.value = ""
            field.display = False
            self.query_one(OptionList).focus()
        else:
            self.exit(None)

    def action_quit_search(self):
        self.exit(None)


async def choose_results(
    results: SearchResults,
    source: str,
    media_type: str,
    query: str,
    *,
    verify_ssl: bool,
):
    """Detect graphics before the UI starts and close network resources on exit."""
    pixel_widget = image_widget()
    app = SearchMenu(
        results,
        source,
        media_type,
        query,
        pixel_widget=pixel_widget,
        verify_ssl=verify_ssl,
    )
    try:
        return await app.run_async()
    finally:
        await app.covers.close()
