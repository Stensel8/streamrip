import os

from rich.console import Console

# Rich's Console.size prefers COLUMNS/LINES env vars over the terminal's
# actual current size, and those don't update when a panel is resized --
# stripping them keeps it reading the real size instead of a stale one.
_environ = {k: v for k, v in os.environ.items() if k not in {"COLUMNS", "LINES"}}
console = Console(_environ=_environ)
# Notices about streamrip itself (a config it created, a newer version), so
# that what a command prints on stdout is its answer alone.
err_console = Console(stderr=True, _environ=_environ)


def plural(count: int, noun: str) -> str:
    """ "1 track", "2 tracks": a count as the terminal says it."""
    return f"{count} {noun}{'s' * (count != 1)}"
