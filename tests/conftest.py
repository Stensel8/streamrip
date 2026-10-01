import os
from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest
from util import arun

import streamrip.media.semaphore as semaphore_module
from streamrip import __version__
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.console import console


@pytest.fixture(scope="session")
def qobuz_client():
    """A logged-in Qobuz client, for the tests that need a real account
    (QOBUZ_USER_ID and QOBUZ_AUTH_TOKEN; they're skipped without them).
    """
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
