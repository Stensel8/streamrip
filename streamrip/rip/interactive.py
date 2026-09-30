"""Prompt/Confirm drop-ins that make Ctrl-C work while blocked on them.

`coro` (rip/cli.py) hands SIGINT to asyncio so Ctrl-C can cancel a running
download cleanly. rich.prompt.Prompt.ask and Confirm.ask block
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


@contextmanager
def _native_sigint():
    """Swap SIGINT to the plain KeyboardInterrupt handler for this block."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        yield  # Not inside an event loop (e.g. a unit test); nothing to swap.
        return

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        # Whatever was installed before -- asyncio's own handler on POSIX,
        # or this same default on Windows, where `coro` never replaces it.
        signal.signal(signal.SIGINT, previous)


class Prompt(_Prompt):
    """rich.prompt.Prompt, but Ctrl-C raises KeyboardInterrupt immediately."""

    @classmethod
    def ask(cls, *args, **kwargs):
        """Same as rich.prompt.Prompt.ask, with Ctrl-C handled natively."""
        with _native_sigint():
            return super().ask(*args, **kwargs)


class Confirm(_Confirm):
    """rich.prompt.Confirm, but Ctrl-C raises KeyboardInterrupt immediately."""

    @classmethod
    def ask(cls, *args, **kwargs):
        """Same as rich.prompt.Confirm.ask, with Ctrl-C handled natively."""
        with _native_sigint():
            return super().ask(*args, **kwargs)
