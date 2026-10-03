import asyncio
import signal

import pytest

from streamrip.rip.interactive import Confirm, Prompt, _native_sigint


async def test_native_sigint_restores_asyncios_handler_afterward():
    """While blocked on input(), Ctrl-C must raise KeyboardInterrupt, the way
    `coro` (rip/cli.py) hands SIGINT to asyncio for cancelling a running
    download -- but Prompt.ask/Confirm.ask block synchronously, so asyncio's
    handler can't run until they return (live bug, 2026-09-30: stuck at a
    menu prompt, Ctrl-C did nothing but echo ^C).
    """
    loop = asyncio.get_running_loop()

    def stop():
        pass

    loop.add_signal_handler(signal.SIGINT, stop)
    installed = signal.getsignal(signal.SIGINT)
    try:
        with _native_sigint():
            assert signal.getsignal(signal.SIGINT) is signal.default_int_handler

        assert signal.getsignal(signal.SIGINT) is installed
    finally:
        loop.remove_signal_handler(signal.SIGINT)


async def test_native_sigint_outside_an_event_loop(monkeypatch):
    """Nothing to swap when there's no asyncio loop to hand SIGINT back to."""
    previous = signal.getsignal(signal.SIGINT)

    def fake_get_running_loop():
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(asyncio, "get_running_loop", fake_get_running_loop)
    with _native_sigint():
        assert signal.getsignal(signal.SIGINT) == previous
    assert signal.getsignal(signal.SIGINT) == previous


async def test_prompt_ask_raises_keyboard_interrupt_on_sigint(monkeypatch):
    def fake_ask(*args, **kwargs):
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        raise KeyboardInterrupt

    monkeypatch.setattr("streamrip.rip.interactive._Prompt.ask", fake_ask)
    with pytest.raises(KeyboardInterrupt):
        Prompt.ask("Choose")


async def test_confirm_ask_raises_keyboard_interrupt_on_sigint(monkeypatch):
    def fake_ask(*args, **kwargs):
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        raise KeyboardInterrupt

    monkeypatch.setattr("streamrip.rip.interactive._Confirm.ask", fake_ask)
    with pytest.raises(KeyboardInterrupt):
        Confirm.ask("Sure?")
