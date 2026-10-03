from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.client.deezer import DeezerClient
from streamrip.config import Config
from streamrip.exceptions import AuthenticationError
from streamrip.rip.deezer_arl_capture import DeezerArlCaptureError
from streamrip.rip.prompter import DeezerPrompter

LOGIN_URL = "https://www.deezer.com/login"


@pytest.fixture
def prompter():
    config = Config.defaults()
    client = DeezerClient(config)
    client.login = AsyncMock()
    return DeezerPrompter(config, client)


@pytest.fixture(autouse=True)
def no_browser_capture(monkeypatch):
    """Browser capture has its own test module; keep it out of these by default."""
    capture = AsyncMock(side_effect=DeezerArlCaptureError("no browser found"))
    monkeypatch.setattr(
        "streamrip.rip.prompter.capture_deezer_arl_via_browser", capture
    )
    return capture


@pytest.fixture
def launch(monkeypatch):
    launch = MagicMock(return_value=0)
    monkeypatch.setattr("streamrip.rip.prompter.launch", launch)
    return launch


def _ask(monkeypatch, *answers):
    prompt = MagicMock(side_effect=list(answers))
    monkeypatch.setattr("streamrip.rip.prompter.Prompt.ask", prompt)
    return prompt


async def test_browser_choice_skips_the_manual_prompt(
    monkeypatch, prompter, no_browser_capture, launch
):
    no_browser_capture.side_effect = None
    no_browser_capture.return_value = "captured-arl"
    prompt = _ask(monkeypatch, "1")

    await prompter.prompt_and_login()
    prompter.save()

    prompt.assert_called_once()
    launch.assert_not_called()
    prompter.client.login.assert_awaited_once()
    assert prompter.config.file.deezer.arl == "captured-arl"


async def test_browser_choice_falls_back_to_entering_it_by_hand(
    monkeypatch, prompter, launch
):
    prompt = _ask(monkeypatch, "1", " typed-arl ")

    await prompter.prompt_and_login()
    prompter.save()

    launch.assert_called_with(LOGIN_URL)
    assert prompt.call_args.kwargs["password"] is True
    assert prompter.config.file.deezer.arl == "typed-arl"


async def test_manual_choice_opens_the_login_page(monkeypatch, prompter, launch):
    _ask(monkeypatch, "2", "typed-arl")

    await prompter.prompt_and_login()
    prompter.save()

    launch.assert_called_with(LOGIN_URL)
    assert prompter.config.file.deezer.arl == "typed-arl"


async def test_no_browser_to_open_is_not_an_error(monkeypatch, prompter, launch):
    launch.side_effect = OSError("No browser available")
    _ask(monkeypatch, "2", "typed-arl")

    await prompter.prompt_and_login()

    assert prompter.config.session.deezer.arl == "typed-arl"


async def test_manual_entry_asks_again_until_it_is_filled(
    monkeypatch, prompter, launch
):
    prompt = _ask(monkeypatch, "2", "", "  ", "typed-arl")

    await prompter.prompt_and_login()

    assert prompter.config.session.deezer.arl == "typed-arl"
    assert prompt.call_count == 4


async def test_saved_arl_skips_every_prompt(monkeypatch, prompter, launch):
    prompter.config.session.deezer.arl = "saved-arl"
    prompt = _ask(monkeypatch)

    await prompter.prompt_and_login()

    prompt.assert_not_called()
    launch.assert_not_called()
    prompter.client.login.assert_awaited_once()


async def test_rejected_arl_offers_the_menu_again(
    monkeypatch, prompter, no_browser_capture, launch
):
    no_browser_capture.side_effect = None
    no_browser_capture.return_value = "arl"
    prompter.client.login.side_effect = [AuthenticationError("expired"), None]
    prompt = _ask(monkeypatch, "1", "1")

    await prompter.prompt_and_login()

    assert prompter.client.login.await_count == 2
    assert prompt.call_count == 2


async def test_menu_offers_no_password_login(monkeypatch, prompter, launch):
    output = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", output)
    _ask(monkeypatch, "2", "typed-arl")

    await prompter.prompt_and_login()

    shown = " ".join(str(c.args[0]) for c in output.call_args_list if c.args).lower()
    assert "email" not in shown and "password" not in shown
