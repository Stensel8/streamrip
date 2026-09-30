import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.rip.prompter import QobuzPrompter
from streamrip.rip.qobuz_token_capture import QobuzTokenCaptureError


@pytest.fixture
def prompter():
    config = Config.defaults()
    client = QobuzClient(config)
    client.login = AsyncMock()
    return QobuzPrompter(config, client)


@pytest.fixture(autouse=True)
def no_snippet_capture(monkeypatch):
    """Snippet auto-capture has its own test module; keep it out of these by default."""
    capture = AsyncMock(side_effect=QobuzTokenCaptureError("timed out"))
    monkeypatch.setattr("streamrip.rip.prompter.capture_qobuz_auth_token", capture)
    return capture


@pytest.fixture(autouse=True)
def no_browser_capture(monkeypatch):
    """Browser auto-capture has its own test module; keep it out of these by default."""
    capture = AsyncMock(side_effect=QobuzTokenCaptureError("no browser found"))
    monkeypatch.setattr(
        "streamrip.rip.prompter.capture_qobuz_auth_token_via_browser", capture
    )
    return capture


@pytest.mark.parametrize("launch_result", [0, 1, OSError("No browser available")])
async def test_default_browser_login_and_manual_credentials(
    monkeypatch, prompter, launch_result
):
    launch = MagicMock()
    if isinstance(launch_result, Exception):
        launch.side_effect = launch_result
    else:
        launch.return_value = launch_result
    monkeypatch.setattr("streamrip.rip.prompter.launch", launch)
    prompt = MagicMock(side_effect=["2", " 123 ", " test-token "])
    monkeypatch.setattr("streamrip.rip.prompter.Prompt.ask", prompt)
    output = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", output)

    await prompter.prompt_and_login()
    prompter.save()

    launch.assert_called_with("https://play.qobuz.com/login")
    assert prompt.call_args.kwargs["password"] is True
    prompter.client.login.assert_awaited_once()
    assert prompter.config.file.qobuz.use_auth_token is True
    assert prompter.config.file.qobuz.email_or_userid == "123"
    assert prompter.config.file.qobuz.password_or_token == "test-token"


async def test_snippet_capture_skips_manual_prompts(
    monkeypatch, prompter, no_snippet_capture
):
    no_snippet_capture.side_effect = None
    no_snippet_capture.return_value = ("456", "captured-token")
    monkeypatch.setattr("streamrip.rip.prompter.launch", MagicMock())
    prompt = MagicMock(side_effect=["2"])
    monkeypatch.setattr("streamrip.rip.prompter.Prompt.ask", prompt)

    await prompter.prompt_and_login()
    prompter.save()

    prompt.assert_called_once()
    prompter.client.login.assert_awaited_once()
    assert prompter.config.file.qobuz.use_auth_token is True
    assert prompter.config.file.qobuz.email_or_userid == "456"
    assert prompter.config.file.qobuz.password_or_token == "captured-token"


async def test_browser_automation_choice_skips_manual_prompts(
    monkeypatch, prompter, no_browser_capture
):
    no_browser_capture.side_effect = None
    no_browser_capture.return_value = ("789", "browser-captured-token")
    monkeypatch.setattr("streamrip.rip.prompter.launch", MagicMock())
    prompt = MagicMock(side_effect=["1"])
    monkeypatch.setattr("streamrip.rip.prompter.Prompt.ask", prompt)

    await prompter.prompt_and_login()
    prompter.save()

    prompt.assert_called_once()
    prompter.client.login.assert_awaited_once()
    assert prompter.config.file.qobuz.use_auth_token is True
    assert prompter.config.file.qobuz.email_or_userid == "789"
    assert prompter.config.file.qobuz.password_or_token == "browser-captured-token"


async def test_saved_credentials_skip_browser(monkeypatch, prompter):
    cfg = prompter.config.session.qobuz
    cfg.use_auth_token = True
    cfg.email_or_userid = "123"
    cfg.password_or_token = "saved-token"
    launch = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.launch", launch)
    prompt = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.Prompt.ask", prompt)
    await prompter.prompt_and_login()
    launch.assert_not_called()
    prompt.assert_not_called()
    prompter.client.login.assert_awaited_once()


async def test_password_fallback_remains_available(monkeypatch, prompter):
    monkeypatch.setattr("streamrip.rip.prompter.launch", MagicMock(return_value=0))
    monkeypatch.setattr(
        "streamrip.rip.prompter.Prompt.ask",
        MagicMock(side_effect=["3", "", "test@example.com", "test-password"]),
    )
    await prompter.prompt_and_login()
    prompter.save()
    cfg = prompter.config.file.qobuz
    assert cfg.use_auth_token is False
    assert cfg.email_or_userid == "test@example.com"
    assert cfg.password_or_token == hashlib.md5(b"test-password").hexdigest()
