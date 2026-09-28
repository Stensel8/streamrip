import os

from rich.console import Console

# Rich's Console.size prefers COLUMNS/LINES env vars over the terminal's
# actual current size, and those don't update when a panel is resized --
# stripping them keeps it reading the real size instead of a stale one.
_environ = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")}
console = Console(_environ=_environ)
