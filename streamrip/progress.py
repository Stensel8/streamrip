from dataclasses import dataclass
from typing import Callable

from rich.console import Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.rule import Rule
from rich.text import Text

from .console import console


class ProgressManager:
    """Owns the Rich Live display shared by every download/resolve progress bar."""

    def __init__(self):
        """Build the resolve and download progress bars, not started yet."""
        self.started = False
        # Its own Progress, not a task in the download one: that one's columns
        # (bar, transfer speed, ETA) don't mean anything for "still fetching
        # metadata" -- this is just a spinner and a line of text.
        self.resolve_progress = Progress(
            SpinnerColumn(), TextColumn("[cyan]{task.description}"), console=console
        )
        # One bar for the artist/label catalog currently running, counting
        # albums rather than bytes -- the per-track rows below only show
        # what's downloading *right now*, not how far into the whole
        # discography that is.
        self.source_progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.0f}%",
            "•",
            TextColumn("{task.completed}/{task.total} albums"),
            console=console,
        )
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            TransferSpeedColumn(),
            "•",
            TimeRemainingColumn(),
            console=console,
        )

        # Keyed by id(), not title text: two releases can share a title.
        self.task_titles: dict[int, str] = {}
        self.prefix = Text.assemble(("Downloading ", "bold cyan"), overflow="ellipsis")
        self._text_cache = self.gen_title_text()
        # Explicit console=console: otherwise Live draws through Rich's own
        # default console, a different object than the one console.log() and
        # the logger's RichHandler print through, so neither knows the other
        # is using the terminal -- each print corrupts the other's region
        # instead of Rich's usual "pause the live area, print above it".
        # transient: erased when stopped, rather than leaving its last frame --
        # by then an empty "Downloading" rule -- under the run's output.
        self.live = Live(
            self._group(), console=console, refresh_per_second=10, transient=True
        )

    def _group(self) -> Group:
        """Return the renderable group the Live display shows."""
        return Group(
            self.get_title_text(),
            self.source_progress,
            self.resolve_progress,
            self.progress,
        )

    def _ensure_started(self):
        """Start the Live display on its first use."""
        if not self.started:
            self.live.start()
            self.started = True

    def _refresh(self):
        """Redraw the Live display, if it has been started."""
        if self.started:
            self.live.update(self._group())

    def get_callback(self, total: int, desc: str):
        """Return a Handle that drives a new download progress bar task."""
        self._ensure_started()

        task = self.progress.add_task(f"[cyan]{desc}", total=total)

        def _callback_update(x: int):
            """Advance the task by x and redraw."""
            self.progress.update(task, advance=x)
            self._refresh()

        def _callback_done():
            """Hide the task once its download is done."""
            self.progress.update(task, visible=False)

        return Handle(_callback_update, _callback_done)

    def get_source_callback(self, total: int, desc: str):
        """Return a Handle that drives a new artist/label album-count bar."""
        self._ensure_started()

        task = self.source_progress.add_task(f"[cyan]{desc}", total=total)

        def _callback_advance(x: int):
            """Advance the task by x and redraw."""
            self.source_progress.update(task, advance=x)
            self._refresh()

        def _callback_done():
            """Hide the task once the catalog is done."""
            self.source_progress.update(task, visible=False)

        return Handle(_callback_advance, _callback_done)

    def get_resolve_callback(self, desc: str):
        """Return a Handle that drives a new resolve spinner task."""
        self._ensure_started()

        task = self.resolve_progress.add_task(f"[cyan]{desc}")
        self._refresh()

        def _done():
            """Hide the task once resolving is done."""
            self.resolve_progress.update(task, visible=False)

        return Handle(lambda _: None, _done)

    def cleanup(self):
        if self.started:
            self.live.stop()

    def clear_screen(self):
        """Wipe the terminal; the live display restarts on its next use."""
        if not console.is_terminal:
            return
        # Stopped first: Live keeps redrawing relative to where it last drew,
        # which a clear underneath it would leave pointing at the wrong lines.
        if self.started:
            self.live.stop()
            self.started = False
        console.clear()

    def add_title(self, key: int, title: str):
        """Show title as the active album/playlist under the given key."""
        self._ensure_started()
        self.task_titles[key] = title.strip()
        self._text_cache = self.gen_title_text()
        self._refresh()

    def remove_title(self, key: int):
        """Stop showing the title registered under the given key."""
        self.task_titles.pop(key, None)
        self._text_cache = self.gen_title_text()
        self._refresh()

    def gen_title_text(self) -> Rule:
        """Render the currently active title(s) as a Rule."""
        # A specific name is only trustworthy when it's the only one active:
        # with several albums in flight (several artists selected at once),
        # which one actually has tracks moving in the progress list below has
        # nothing to do with which one's title was added last, so naming that
        # one here just claims the wrong album is what's downloading. Each
        # track row already carries its own artist/album, so once there's
        # more than one, a plain count is the only claim this line can back up.
        titles = list(self.task_titles.values())
        if len(titles) > 1:
            shown = f"{len(titles)} albums/playlists"
        else:
            shown = titles[0] if titles else ""
        t = self.prefix + Text(shown)
        return Rule(t)

    def get_title_text(self) -> Rule:
        return self._text_cache


@dataclass(slots=True)
class Handle:
    update: Callable[[int], None]
    done: Callable[[], None]

    def __enter__(self):
        return self.update

    def __exit__(self, *_):
        self.done()


# global instance
_p = ProgressManager()


def get_progress_callback(enabled: bool, total: int, desc: str) -> Handle:
    global _p
    if not enabled:
        return Handle(lambda _: None, lambda: None)
    return _p.get_callback(total, desc)


def get_source_callback(enabled: bool, total: int, desc: str) -> Handle:
    """Return an artist/label album-count Handle, or a no-op one if disabled."""
    global _p
    if not enabled:
        return Handle(lambda _: None, lambda: None)
    return _p.get_source_callback(total, desc)


def get_resolve_callback(enabled: bool, desc: str) -> Handle:
    """Return a resolve progress Handle, or a no-op one if disabled."""
    global _p
    if not enabled:
        return Handle(lambda _: None, lambda: None)
    return _p.get_resolve_callback(desc)


def add_title(key: int, title: str, enabled: bool = True):
    """Show title as the active album/playlist, unless disabled."""
    if not enabled:
        return
    global _p
    _p.add_title(key, title)


def remove_title(key: int, enabled: bool = True):
    """Stop showing the title registered under key, unless disabled."""
    if not enabled:
        return
    global _p
    _p.remove_title(key)


def clear_screen(enabled: bool = True):
    """Wipe the terminal, unless disabled."""
    if not enabled:
        return
    global _p
    _p.clear_screen()


def clear_progress():
    global _p
    _p.cleanup()
