from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from streamrip.rip import browser_login
from streamrip.rip.browser_login import (
    BrowserLoginError,
    login_page,
    wait_for_login,
)


class MyError(BrowserLoginError):
    pass


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace asyncio.sleep, so waiting takes no time."""
    sleep = AsyncMock()
    monkeypatch.setattr(browser_login.asyncio, "sleep", sleep)
    return sleep


async def test_wait_returns_what_the_probe_finds(no_sleep):
    """The wait calls the probe until it finds something."""
    probe = AsyncMock(side_effect=[None, "", ("id", "token")])

    assert await wait_for_login(probe, 60, MyError) == ("id", "token")
    assert probe.await_count == 3
    assert no_sleep.await_count == 2


async def test_wait_gives_up_with_the_callers_error(monkeypatch):
    """After the timeout the wait raises the caller's own error."""
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
            """Hand out the fake browser."""
            return browser

        async def __aexit__(self, *exc):
            """Close the fake browser."""
            await browser.close()

    monkeypatch.setattr(browser_login, "open_login_browser", lambda *a, **k: Opened())
    return browser, page


async def test_login_page_is_on_the_login_url(opened):
    """The page is open on the login URL, and the browser closes afterwards."""
    browser, page = opened

    async with login_page("Svc", "https://x/login", MyError, "a token") as got:
        assert got is page

    page.goto.assert_awaited_once_with("https://x/login")
    browser.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("raised", "message"),
    [
        (BrowserLoginError("no browser"), "no browser"),
        (PlaywrightError("window closed"), "window closed"),
    ],
)
async def test_login_page_errors_come_out_as_the_callers_own(opened, raised, message):
    """Whatever fails comes out as the caller's error, with the original as its cause."""
    browser, page = opened
    page.goto.side_effect = raised

    with pytest.raises(MyError, match=message) as caught:
        async with login_page("Svc", "https://x/login", MyError, "a token"):
            pass

    assert caught.value.__cause__ is raised
    browser.close.assert_awaited_once()


async def test_the_callers_own_error_is_not_wrapped(opened):
    """An error of the caller's own kind passes through unchanged."""
    error = MyError("no login in time")

    with pytest.raises(MyError) as caught:
        async with login_page("Svc", "https://x/login", MyError, "a token"):
            raise error

    assert caught.value is error
