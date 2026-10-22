"""Prompt/Confirm drop-ins that make Ctrl-C work while blocked on them.

`coro` (rip/cli.py) hands SIGINT and SIGTERM to asyncio so Ctrl-C and a
`podman stop` can cancel a running download cleanly. rich.prompt.Prompt.ask and Confirm.ask block
synchronously on input() though, and asyncio's handler can't run until
that call returns -- so while blocked on one of them, Ctrl-C does nothing
but echo ^C into the input line, until something else hands control back
to the event loop (observed live 2026-09-30: stuck at a menu prompt,
accumulating ^C with no effect, then a burst of "Stopping..." once it
finally got through, after code past the prompt had already run).

Importing Prompt/Confirm from here instead of rich.prompt swaps back to
the plain KeyboardInterrupt-on-Ctrl-C behavior only while actually
blocked on input, with no other change at the call site.
"""

import asyncio
import signal
from contextlib import contextmanager

from rich.prompt import Confirm as _Confirm
from rich.prompt import Prompt as _Prompt

from .stopping import terminate


@contextmanager
def _native_signals():
    """Make Ctrl-C and SIGTERM act at once for this block.

    SIGINT gets the plain KeyboardInterrupt handler, SIGTERM one that exits.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        yield  # Not inside an event loop (e.g. a unit test); nothing to swap.
        return

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        # Whatever was installed before -- asyncio's own handlers on POSIX,
        # or the defaults on Windows, where `coro` never replaces them.
        signal.signal(signal.SIGINT, previous_int)
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


class Prompt(_Prompt):
    """rich.prompt.Prompt, but Ctrl-C raises KeyboardInterrupt immediately."""

    @classmethod
    def ask(cls, *args, **kwargs):
        """Same as rich.prompt.Prompt.ask, with Ctrl-C handled natively."""
        with _native_signals():
            return super().ask(*args, **kwargs)


class Confirm(_Confirm):
    """rich.prompt.Confirm, but Ctrl-C raises KeyboardInterrupt immediately."""

    @classmethod
    def ask(cls, *args, **kwargs):
        """Same as rich.prompt.Confirm.ask, with Ctrl-C handled natively."""
        with _native_signals():
            return super().ask(*args, **kwargs)
