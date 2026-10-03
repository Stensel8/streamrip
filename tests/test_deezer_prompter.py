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
    """A Deezer prompter whose client logs in without a network."""
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
    """Replace the launch of the browser for the login page."""
    launch = MagicMock(return_value=0)
    monkeypatch.setattr("streamrip.rip.prompter.launch", launch)
    return launch


def _ask(monkeypatch, *answers):
    """Answer the prompts with `answers`, in order."""
    prompt = MagicMock(side_effect=list(answers))
    monkeypatch.setattr("streamrip.rip.prompter.Prompt.ask", prompt)
    return prompt


async def test_browser_choice_skips_the_manual_prompt(
    monkeypatch, prompter, no_browser_capture, launch
):
    """Choosing the browser and getting an ARL from it needs no manual entry."""
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
    """If the browser capture fails, the ARL is asked for by hand."""
    prompt = _ask(monkeypatch, "1", " typed-arl ")

    await prompter.prompt_and_login()
    prompter.save()

    launch.assert_called_with(LOGIN_URL)
    assert prompt.call_args.kwargs["password"] is True
    assert prompter.config.file.deezer.arl == "typed-arl"


async def test_manual_choice_opens_the_login_page(monkeypatch, prompter, launch):
    """Choosing manual entry opens the login page and asks for the ARL."""
    _ask(monkeypatch, "2", "typed-arl")

    await prompter.prompt_and_login()
    prompter.save()

    launch.assert_called_with(LOGIN_URL)
    assert prompter.config.file.deezer.arl == "typed-arl"


async def test_no_browser_to_open_is_not_an_error(monkeypatch, prompter, launch):
    """Failing to open the login page doesn't stop manual entry."""
    launch.side_effect = OSError("No browser available")
    _ask(monkeypatch, "2", "typed-arl")

    await prompter.prompt_and_login()

    assert prompter.config.session.deezer.arl == "typed-arl"


async def test_manual_entry_asks_again_until_it_is_filled(
    monkeypatch, prompter, launch
):
    """A blank ARL is asked for again."""
    prompt = _ask(monkeypatch, "2", "", "  ", "typed-arl")

    await prompter.prompt_and_login()

    assert prompter.config.session.deezer.arl == "typed-arl"
    assert prompt.call_count == 4


async def test_saved_arl_skips_every_prompt(monkeypatch, prompter, launch):
    """A saved ARL is used without any prompt."""
    prompter.config.session.deezer.arl = "saved-arl"
    prompt = _ask(monkeypatch)

    await prompter.prompt_and_login()

    prompt.assert_not_called()
    launch.assert_not_called()
    prompter.client.login.assert_awaited_once()


async def test_rejected_arl_offers_the_menu_again(
    monkeypatch, prompter, no_browser_capture, launch
):
    """An ARL Deezer rejects brings the menu back."""
    no_browser_capture.side_effect = None
    no_browser_capture.return_value = "arl"
    prompter.client.login.side_effect = [AuthenticationError("expired"), None]
    prompt = _ask(monkeypatch, "1", "1")

    await prompter.prompt_and_login()

    assert prompter.client.login.await_count == 2
    assert prompt.call_count == 2


async def test_menu_offers_no_password_login(monkeypatch, prompter, launch):
    """Nothing in the menu or the instructions mentions an email or password login."""
    output = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", output)
    _ask(monkeypatch, "2", "typed-arl")

    await prompter.prompt_and_login()

    shown = " ".join(str(c.args[0]) for c in output.call_args_list if c.args).lower()
    assert "email" not in shown and "password" not in shown


async def test_manual_entry_is_the_default_choice(monkeypatch, prompter, launch):
    """Manual entry is the default choice.

    The browser option may have to download a browser first (about 150 MB): it's
    offered, but never the answer a bare Enter gives.
    """
    prompt = _ask(monkeypatch, "2", "typed-arl")

    await prompter.prompt_and_login()

    assert prompt.call_args_list[0].kwargs["default"] == "2"


def test_deezer_and_qobuz_announce_a_save_the_same_way(monkeypatch, prompter):
    """Both prompters say the same thing when they save."""
    from streamrip.client.qobuz import QobuzClient
    from streamrip.rip.prompter import QobuzPrompter

    qobuz = QobuzPrompter(prompter.config, QobuzClient(prompter.config))
    output = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", output)

    prompter.save()
    deezer_says = output.call_args.args[0]
    qobuz.save()
    qobuz_says = output.call_args.args[0]

    assert deezer_says == qobuz_says
    assert "saved to config file at" in deezer_says
