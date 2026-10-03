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


@dataclass(slots=True)
class Handle:
    """A progress task: `with handle as update:` advances it, and leaving
    the block hides it.
    """

    update: Callable[[int], None]
    done: Callable[[], None]

    def __enter__(self):
        return self.update

    def __exit__(self, *_):
        self.done()


# What every progress function hands out when progress bars are disabled.
NO_PROGRESS = Handle(lambda _: None, lambda: None)


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
        self._title = self._title_rule()
        # Explicit console=console: otherwise Live draws through Rich's own
        # default console, a different object than the one console.log() and
        # the logger's RichHandler print through, so neither knows the other
        # is using the terminal -- each print corrupts the other's region
        # instead of Rich's usual "pause the live area, print above it".
        # transient: erased when stopped, rather than leaving its last frame --
        # by then an empty "Downloading" rule -- under the run's output.
        # The Live redraws itself 10 times a second, progress bars included;
        # only a new title needs a new renderable.
        self.live = Live(
            self._group(), console=console, refresh_per_second=10, transient=True
        )

    def _group(self) -> Group:
        """Return the renderable group the Live display shows."""
        return Group(
            self._title, self.source_progress, self.resolve_progress, self.progress
        )

    def _ensure_started(self):
        """Start the Live display on its first use."""
        if not self.started:
            self.live.update(self._group())
            self.live.start()
            self.started = True

    def _add_task(self, progress: Progress, desc: str, **task) -> Handle:
        """Add a task to one of the progress bars, hidden again when done."""
        self._ensure_started()
        task_id = progress.add_task(f"[cyan]{desc}", **task)
        return Handle(
            lambda n: progress.update(task_id, advance=n),
            lambda: progress.update(task_id, visible=False),
        )

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
        self._update_title()

    def remove_title(self, key: int):
        """Stop showing the title registered under the given key."""
        self.task_titles.pop(key, None)
        self._update_title()

    def _update_title(self):
        self._title = self._title_rule()
        if self.started:
            self.live.update(self._group())

    def _title_rule(self) -> Rule:
        """Render the currently active title(s) as a Rule."""
        # A specific name is only trustworthy when it's the only one active:
        # with several albums in flight, which one has tracks moving in the
        # list below has nothing to do with whose title was added last. Each
        # track row names its own artist, so several get a plain count.
        titles = list(self.task_titles.values())
        if len(titles) > 1:
            shown = f"{len(titles)} albums/playlists"
        else:
            shown = titles[0] if titles else ""
        prefix = Text.assemble(("Downloading ", "bold cyan"), overflow="ellipsis")
        return Rule(prefix + Text(shown))


_p = ProgressManager()


def get_progress_callback(enabled: bool, total: int, desc: str) -> Handle:
    """Return a download progress Handle, or a no-op one if disabled.

    A total of 0 means the size is unknown. Rich takes total=0 for a finished
    task (a full bar, no spinner), so it becomes None, a pulsing bar.
    """
    if not enabled:
        return NO_PROGRESS
    return _p._add_task(_p.progress, desc, total=total or None)


def get_source_callback(enabled: bool, total: int, desc: str) -> Handle:
    """Return an artist/label album-count Handle, or a no-op one if disabled."""
    if not enabled:
        return NO_PROGRESS
    return _p._add_task(_p.source_progress, desc, total=total)


def get_resolve_callback(enabled: bool, desc: str) -> Handle:
    """Return a resolve progress Handle, or a no-op one if disabled."""
    return _p._add_task(_p.resolve_progress, desc) if enabled else NO_PROGRESS


def add_title(key: int, title: str, enabled: bool = True):
    """Show title as the active album/playlist, unless disabled."""
    if enabled:
        _p.add_title(key, title)


def remove_title(key: int, enabled: bool = True):
    """Stop showing the title registered under key, unless disabled."""
    if enabled:
        _p.remove_title(key)


def clear_screen(enabled: bool = True):
    """Wipe the terminal, unless disabled."""
    if enabled:
        _p.clear_screen()


def clear_progress():
    """Stop the live display, if it was started."""
    if _p.started:
        _p.live.stop()
