import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from streamrip.exceptions import AuthenticationError
from streamrip.rip import browser_login
from streamrip.rip.browser_login import BrowserLoginError, login_page, wait_for_login
from streamrip.rip.interactive import Confirm, Prompt, _native_signals
from streamrip.rip.main import Main

# --- waiting for a login in a browser window ----------------------------------


class MyError(BrowserLoginError):
    pass


@pytest.mark.login
async def test_wait_returns_what_the_probe_finds(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(browser_login.asyncio, "sleep", sleep)
    probe = AsyncMock(side_effect=[None, "", ("id", "token")])

    assert await wait_for_login(probe, 60, MyError) == ("id", "token")
    assert probe.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.login
async def test_wait_gives_up_with_the_callers_error(monkeypatch):
    ticks = iter(range(0, 1000, 20))
    loop = browser_login.asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", lambda: next(ticks))
    monkeypatch.setattr(browser_login.asyncio, "sleep", AsyncMock())

    with pytest.raises(MyError, match="within 60s"):
        await wait_for_login(AsyncMock(return_value=None), 60, MyError)


@pytest.fixture
def opened(monkeypatch):
    """A fake browser handed out by open_login_browser."""
    browser = AsyncMock()
    page = browser.new_context.return_value.new_page.return_value

    class Opened:
        async def __aenter__(self):
            return browser

        async def __aexit__(self, *exc):
            await browser.close()

    monkeypatch.setattr(browser_login, "open_login_browser", lambda *a, **k: Opened())
    return browser, page


@pytest.mark.login
@pytest.mark.parametrize(
    "raised", [BrowserLoginError("no browser"), PlaywrightError("window closed")]
)
async def test_login_page_errors_come_out_as_the_callers_own(opened, raised):
    browser, page = opened
    page.goto.side_effect = raised

    with pytest.raises(MyError, match=str(raised)) as caught:
        async with login_page("Svc", "https://x/login", MyError, "a token"):
            pass

    assert caught.value.__cause__ is raised
    browser.close.assert_awaited_once()


@pytest.mark.login
async def test_the_callers_own_error_is_not_wrapped(opened):
    error = MyError("no login in time")

    with pytest.raises(MyError) as caught:
        async with login_page("Svc", "https://x/login", MyError, "a token"):
            raise error

    assert caught.value is error


@pytest.mark.login
@pytest.mark.parametrize(
    ("platform", "env", "installed", "expected"),
    [
        ("linux", {"DISPLAY": ":0"}, True, True),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True, True),
        ("linux", {}, True, False),  # a container, or an SSH session
        ("linux", {"DISPLAY": ":0"}, False, False),  # the browser extra is not there
        ("darwin", {}, True, True),
        ("win32", {}, True, True),
    ],
    ids=["x11", "wayland", "no display", "no playwright", "macos", "windows"],
)
def test_the_browser_window_is_only_available_where_it_can_work(
    monkeypatch, platform, env, installed, expected
):
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(browser_login.sys, "platform", platform)
    monkeypatch.setattr(
        browser_login.importlib.util,
        "find_spec",
        lambda name: object() if installed else None,
    )

    assert browser_login.browser_login_available() is expected


# --- a saved login that stopped working ---------------------------------------
# has_creds() only checks that something is stored, so an expired token fails
# in login(): that used to be a traceback, with the prompter to fix it at hand.


@pytest.fixture
def expired(monkeypatch):
    """A Main whose saved Tidal login has expired, and its client and prompter."""
    main = Main.__new__(Main)
    client = MagicMock(logged_in=False)
    client.login = AsyncMock(
        side_effect=AuthenticationError("Tidal refresh token has expired.")
    )
    main.clients = {"tidal": client}
    main.config = MagicMock()
    main._announced = set()

    prompter = MagicMock()
    prompter.has_creds.return_value = True
    prompter.prompt_and_login = AsyncMock(
        side_effect=lambda: setattr(client, "logged_in", True)
    )
    monkeypatch.setattr("streamrip.rip.main.get_prompter", lambda *_: prompter)
    return main, client, prompter


def _answer(monkeypatch, *, isatty: bool, confirm: bool):
    monkeypatch.setattr("sys.stdin.isatty", lambda: isatty)
    monkeypatch.setattr("streamrip.rip.main.Confirm.ask", lambda *a, **k: confirm)


@pytest.mark.login
async def test_an_expired_login_is_renewed_when_the_user_agrees(monkeypatch, expired):
    main, client, prompter = expired
    _answer(monkeypatch, isatty=True, confirm=True)
    assert await main.get_logged_in_client("tidal") is client
    prompter.prompt_and_login.assert_awaited_once()
    prompter.save.assert_called_once()


@pytest.mark.login
@pytest.mark.parametrize(
    ("isatty", "confirm", "error"),
    [
        (True, False, "refresh token has expired"),
        # From cron or a script, a hidden y/n would hang forever.
        (False, True, "interactive terminal"),
    ],
    ids=["declined", "no terminal"],
)
async def test_an_expired_login_is_raised_otherwise(
    monkeypatch, expired, isatty, confirm, error
):
    main, _, prompter = expired
    _answer(monkeypatch, isatty=isatty, confirm=confirm)
    with pytest.raises(AuthenticationError, match=error):
        await main.get_logged_in_client("tidal")
    prompter.prompt_and_login.assert_not_awaited()


# --- signals at a prompt ------------------------------------------------------


async def test_at_a_prompt_ctrl_c_and_sigterm_act_at_once():
    """Prompt.ask and Confirm.ask block, so the asyncio handlers `coro` (rip/cli.py)
    installs can't run until they return (seen live 2026-09-30: Ctrl-C at a menu
    only echoed ^C; `podman stop` sat out its timeout)."""
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: None)
    loop.add_signal_handler(signal.SIGTERM, lambda: None)
    installed = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    try:
        with _native_signals():
            assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
            with pytest.raises(SystemExit) as ended:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            assert ended.value.code == 128 + signal.SIGTERM

        restored = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        assert restored == installed
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)


def test_native_signals_outside_an_event_loop():
    """Nothing to swap when there's no asyncio loop to hand SIGINT back to."""
    previous = signal.getsignal(signal.SIGINT)
    with _native_signals():
        assert signal.getsignal(signal.SIGINT) == previous
    assert signal.getsignal(signal.SIGINT) == previous


@pytest.mark.parametrize(("ask", "rich"), [(Prompt, "_Prompt"), (Confirm, "_Confirm")])
async def test_ask_raises_keyboard_interrupt_on_sigint(monkeypatch, ask, rich):
    def fake_ask(*args, **kwargs):
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        raise KeyboardInterrupt

    monkeypatch.setattr(f"streamrip.rip.interactive.{rich}.ask", fake_ask)
    with pytest.raises(KeyboardInterrupt):
        ask.ask("Sure?")
