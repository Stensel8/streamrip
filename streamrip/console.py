import os

from rich.console import Console
from rich.theme import Theme

# Rich's Console.size prefers COLUMNS/LINES env vars over the terminal's
# actual current size, and those don't update when a panel is resized --
# stripping them keeps it reading the real size instead of a stale one.
_environ = {k: v for k, v in os.environ.items() if k not in {"COLUMNS", "LINES"}}
# The progress bars in the colours of streamrip's logo: Rich's own names for
# them, so every bar and its columns follow.
THEME = Theme(
    {
        "bar.complete": "#ac83d0",
        "bar.pulse": "#ac83d0",
        "bar.finished": "#4dbfc8",
        "progress.spinner": "#ac83d0",
        "progress.percentage": "#ac83d0",
        "progress.elapsed": "#4dbfc8",
        "progress.remaining": "#4dbfc8",
        "progress.download": "#4dbfc8",
        "progress.data.speed": "#4dbfc8",
    }
)
console = Console(_environ=_environ, theme=THEME)
# Notices about streamrip itself (a config it created, a newer version), so
# that what a command prints on stdout is its answer alone.
err_console = Console(stderr=True, _environ=_environ, theme=THEME)


def plural(count: int, noun: str) -> str:
    """ "1 track", "2 tracks": a count as the terminal says it."""
    return f"{count} {noun}{'s' * (count != 1)}"
