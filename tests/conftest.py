import os
from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from util import arun

import streamrip.media.semaphore as semaphore_module
from streamrip import __version__
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.console import console
from streamrip.rip.interactive import Confirm


@pytest.fixture(scope="session")
def qobuz_client():
    """A logged-in Qobuz client, for the tests that need a real account
    (QOBUZ_USER_ID and QOBUZ_AUTH_TOKEN; they're skipped without them).
    """
    if "QOBUZ_USER_ID" not in os.environ or "QOBUZ_AUTH_TOKEN" not in os.environ:
        pytest.skip("Qobuz user ID and auth token are required.")
    config = Config.defaults()
    config.session.qobuz.user_id = os.environ["QOBUZ_USER_ID"]
    config.session.qobuz.auth_token = os.environ["QOBUZ_AUTH_TOKEN"]
    if "QOBUZ_APP_ID" in os.environ and "QOBUZ_SECRETS" in os.environ:
        config.session.qobuz.app_id = os.environ["QOBUZ_APP_ID"]
        config.session.qobuz.secrets = os.environ["QOBUZ_SECRETS"].split(",")
    client = QobuzClient(config)
    arun(client.login())

    yield client

    arun(client.session.close())


@pytest.fixture(autouse=True)
def _reset_global_download_semaphore():
    """`global_download_semaphore` caches one max_connections value for the
    whole process, by design: a real streamrip run has exactly one config.
    Tests construct many different Config instances, so without a reset the
    first test to touch it locks in its value and every later test using a
    different max_connections fails its consistency assert.
    """
    semaphore_module._global_semaphore = None
    yield
    semaphore_module._global_semaphore = None


@pytest.fixture(autouse=True)
def _no_real_update_check(monkeypatch):
    """rip() (rip/cli.py) checks for a newer release before every command.

    Left live, CliRunner-driven tests would hit the real GitHub API on
    every single invocation -- slow, flaky, and (confirmed live
    2026-09-30) liable to pick up whatever real or test release happens
    to exist there at the time, polluting unrelated tests' output. Tests
    that specifically exercise the update check override this themselves.
    """
    monkeypatch.setattr(
        "streamrip.rip.cli.latest_streamrip_version",
        AsyncMock(return_value=(__version__, None, False)),
    )


@pytest.fixture(autouse=True)
def _no_real_lyrics_lookup(monkeypatch):
    """A track whose source sent no lyrics is looked up on lrclib.net (lyrics.py).

    Left live, every test that downloads such a track would ask the real service
    about its made-up artist and title, in every CI run, and leave a session
    behind that the next test's event loop trips over. The tests of the lookup
    itself (test_lyrics.py) have a server of their own to ask.
    """
    monkeypatch.setattr(
        "streamrip.media.track.lyrics.find_lyrics", AsyncMock(return_value=None)
    )


@pytest.fixture(autouse=True)
def _no_live_spinners(monkeypatch):
    """console.status(...) (login, resolving, searching, update-check
    spinners) opens a Rich Live display. Confirmed live 2026-09-30: merely
    entering and exiting one, independent of what runs inside it, leaves
    Console in a state where anything it prints afterward no longer reaches
    Click's CliRunner output capture -- so a test can trigger a spinner
    somewhere in the middle of a run and see every later assertion on
    result.output fail, for a reason with nothing to do with what the test
    is actually checking. Swap it for a plain no-op context manager.
    """
    monkeypatch.setattr(console, "status", lambda *a, **k: nullcontext())


@pytest.fixture
async def serve():
    """Start a local server on a free port: `await serve({"/path": handler})`.

    Gives its base URL, ending in a slash. Every server started is stopped after
    the test.
    """
    runners = []

    async def start(handlers: dict) -> str:
        app = web.Application()
        for path, handler in handlers.items():
            app.router.add_get(path, handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        runners.append(runner)
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        host, port = runner.addresses[0][:2]
        return f"http://{host}:{port}/"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.fixture(scope="session")
def _installed_browser() -> str | None:
    """The Chrome-family browser the real-browser tests drive, if there is one."""
    from playwright.async_api import async_playwright

    from streamrip.rip.browser_login import launch_installed_chromium

    async def probe():
        async with async_playwright() as pw:
            browser, name = await launch_installed_chromium(pw, headless=True)
            if browser is not None:
                await browser.close()
            return name

    return arun(probe())


def _no_prompt(*_args, **_kwargs):
    pytest.fail("A test must never wait for an answer on stdin", pytrace=False)


@pytest.fixture(autouse=True)
def _real_browser(request, monkeypatch):
    """Run a `real_browser` test only where a browser is installed.

    Without one, the code under test stops to ask on stdin whether to download
    Playwright's own, which no test can answer. Such a test is skipped, and
    fails on CI (GitHub's runners have Chrome): there, a skip would quietly
    turn the coverage off if the runner image ever lost it.
    """
    if request.node.get_closest_marker("real_browser") is None:
        return
    if request.getfixturevalue("_installed_browser") is None:
        message = (
            "needs a Chrome-family browser (Chrome, Edge, Chromium, Brave...) "
            "installed; deselect these tests with -m 'not real_browser'"
        )
        if os.environ.get("CI"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    # With a browser found, a prompt means something is wrong: fail, don't hang.
    monkeypatch.setattr(Confirm, "ask", staticmethod(_no_prompt))
