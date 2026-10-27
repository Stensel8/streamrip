"""Ctrl-C and SIGTERM stop a running command, cleaning up first."""

import asyncio
import contextlib
import os
import signal

import pytest
from click.testing import CliRunner

from streamrip.config import set_user_defaults
from streamrip.rip.cli import coro, rip
from streamrip.rip.stopping import terminate

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="asyncio signal handlers only exist on POSIX"
)


def _waiting_command(signum, seen):
    """A command that is sent `signum` while it waits, and records how it ended."""

    @coro
    async def command():
        # A signal without a handler would end the whole test run: look first.
        assert signal.getsignal(signum) not in {signal.SIG_DFL, signal.SIG_IGN}
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signum)
        try:
            await asyncio.sleep(30)
        finally:
            seen["cleaned_up"] = True
            # A second signal has to act as usual again.
            seen["sigterm"] = signal.getsignal(signal.SIGTERM)
            seen["sigint"] = signal.getsignal(signal.SIGINT)

    return command


def test_sigterm_cancels_the_command_and_exits_143():
    seen = {}

    with pytest.raises(SystemExit) as stopped:
        _waiting_command(signal.SIGTERM, seen)()

    assert stopped.value.code == 128 + signal.SIGTERM
    assert seen["cleaned_up"]
    assert seen["sigterm"] is signal.SIG_DFL
    assert seen["sigint"] is signal.default_int_handler


def test_ctrl_c_cancels_the_command_and_ends_normally():
    seen = {}

    assert _waiting_command(signal.SIGINT, seen)() is None

    assert seen["cleaned_up"]
    assert seen["sigterm"] is signal.SIG_DFL
    assert seen["sigint"] is signal.default_int_handler


def test_the_handlers_are_gone_once_the_command_has_ended():
    previous = signal.getsignal(signal.SIGTERM)

    @coro
    async def quick():
        return None

    quick()

    assert signal.getsignal(signal.SIGTERM) == previous


def test_sigterm_exits_143_even_when_the_command_swallows_the_cancellation():
    """A screen that catches the cancellation still ends as a SIGTERM did."""

    @coro
    async def command():
        asyncio.get_running_loop().call_later(
            0.05, os.kill, os.getpid(), signal.SIGTERM
        )
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(30)

    with pytest.raises(SystemExit) as stopped:
        command()

    assert stopped.value.code == 128 + signal.SIGTERM


def test_a_plain_command_ends_on_sigterm_only_while_it_runs(tmp_path, monkeypatch):
    """Commands without an event loop, and their prompts, need it too."""
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    seen = {}

    def probe():
        seen["during"] = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(rip.commands["config"].commands["path"], "callback", probe)
    before = signal.getsignal(signal.SIGTERM)

    result = CliRunner().invoke(rip, ["--config-path", str(path), "config", "path"])

    assert result.exit_code == 0, result.output
    assert seen["during"] is terminate
    assert signal.getsignal(signal.SIGTERM) == before
