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
    def __init__(self):
        self.started = False
        # Its own Progress, not a task in the download one: that one's columns
        # (bar, transfer speed, ETA) don't mean anything for "still fetching
        # metadata" -- this is just a spinner and a line of text.
        self.resolve_progress = Progress(
            SpinnerColumn(), TextColumn("[cyan]{task.description}"), console=console
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
        self.live = Live(self._group(), console=console, refresh_per_second=10)

    def _group(self) -> Group:
        return Group(self.get_title_text(), self.resolve_progress, self.progress)

    def _ensure_started(self):
        if not self.started:
            self.live.start()
            self.started = True

    def _refresh(self):
        if self.started:
            self.live.update(self._group())

    def get_callback(self, total: int, desc: str):
        self._ensure_started()

        task = self.progress.add_task(f"[cyan]{desc}", total=total)

        def _callback_update(x: int):
            self.progress.update(task, advance=x)
            self._refresh()

        def _callback_done():
            self.progress.update(task, visible=False)

        return Handle(_callback_update, _callback_done)

    def get_resolve_callback(self, desc: str):
        self._ensure_started()

        task = self.resolve_progress.add_task(f"[cyan]{desc}")
        self._refresh()

        def _done():
            self.resolve_progress.update(task, visible=False)

        return Handle(lambda _: None, _done)

    def cleanup(self):
        if self.started:
            self.live.stop()

    def add_title(self, key: int, title: str):
        self._ensure_started()
        self.task_titles[key] = title.strip()
        self._text_cache = self.gen_title_text()
        self._refresh()

    def remove_title(self, key: int):
        self.task_titles.pop(key, None)
        self._text_cache = self.gen_title_text()
        self._refresh()

    def gen_title_text(self) -> Rule:
        # One name, not several joined by commas: two albums on one line read
        # as one confusing thing, not two. Dict order is insertion order, so
        # this is the most recently started album/playlist still active.
        titles = list(self.task_titles.values())
        shown = titles[-1] if titles else ""
        if len(titles) > 1:
            shown += f" (+{len(titles) - 1} more)"
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


def get_resolve_callback(enabled: bool, desc: str) -> Handle:
    global _p
    if not enabled:
        return Handle(lambda _: None, lambda: None)
    return _p.get_resolve_callback(desc)


def add_title(key: int, title: str, enabled: bool = True):
    if not enabled:
        return
    global _p
    _p.add_title(key, title)


def remove_title(key: int, enabled: bool = True):
    if not enabled:
        return
    global _p
    _p.remove_title(key)


def clear_progress():
    global _p
    _p.cleanup()
