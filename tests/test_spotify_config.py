"""The [spotify] section, and the prompter that fills it."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import tomlkit

from streamrip.client import SpotifyClient
from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import AuthenticationError
from streamrip.rip import prompter as prompter_module
from streamrip.rip.prompter import PROMPTERS, SpotifyPrompter, get_prompter
from streamrip.rip.spotify_login import SpotifyLoginError

# --- the config --------------------------------------------------------------


@pytest.mark.login
def test_spotify_credentials_are_written_when_the_config_is_saved(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(path)

    with Config(path) as config:
        config.file.spotify.client_id = "abc"
        config.file.spotify.refresh_token = "r"
        config.file.set_modified()

    saved = tomlkit.parse(path.read_text())
    assert saved["spotify"]["client_id"] == "abc"
    assert saved["spotify"]["refresh_token"] == "r"
    assert saved["spotify"]["audio_format"] == "m4a"


# --- the prompter ------------------------------------------------------------


@pytest.fixture
def spotify_prompter():
    config = Config.defaults()
    client = SpotifyClient(config)
    return SpotifyPrompter(config, client), config, client


@pytest.mark.login
def test_the_prompter_is_registered_for_spotify(spotify_prompter):
    _, config, client = spotify_prompter

    assert PROMPTERS["spotify"] is SpotifyPrompter
    assert isinstance(get_prompter(client, config), SpotifyPrompter)


@pytest.mark.login
@pytest.mark.parametrize(
    ("client_id", "refresh_token", "has"),
    [("", "", False), ("cid", "", False), ("", "r", False), ("cid", "r", True)],
)
def test_a_login_needs_the_app_and_a_refresh_token(
    spotify_prompter, client_id, refresh_token, has
):
    prompter, config, _ = spotify_prompter
    config.session.spotify.client_id = client_id
    config.session.spotify.refresh_token = refresh_token

    assert prompter.has_creds() is has


class Answers:
    """Plays back the answers to Prompt.ask, in order, and keeps the questions."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question, **_):
        self.questions.append(question)
        return self.answers.pop(0)


async def test_the_app_is_explained_then_the_client_id_asked(
    spotify_prompter, monkeypatch
):
    prompter, config, client = spotify_prompter
    printed = []
    monkeypatch.setattr(
        prompter_module.console, "print", lambda *a, **k: printed.append(a)
    )
    monkeypatch.setattr(prompter_module, "_open_login_link", lambda url: None)
    answers = Answers(
        "  my-client-id  ", "2", "http://127.0.0.1:9900/callback?code=C&state=S"
    )
    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(answers))
    client.authorization_url = lambda: (
        "https://accounts.spotify.com/authorize?x",
        "S",
        "V",
    )
    client.finish_login = AsyncMock()

    await prompter.prompt_and_login()

    assert config.session.spotify.client_id == "my-client-id"
    client.finish_login.assert_awaited_once_with("C", "V")
    explanation = " ".join(str(part) for call in printed for part in call)
    assert "Premium" in explanation
    assert "developer.spotify.com/dashboard" in explanation
    assert "Web API" in explanation
    assert "http://127.0.0.1:9900/callback" in explanation
    assert answers.questions[0] == "Enter the Client ID of your Spotify app"


async def test_a_saved_client_id_is_not_asked_again(spotify_prompter, monkeypatch):
    prompter, config, client = spotify_prompter
    config.session.spotify.client_id = "saved"
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    answers = Answers("2", "http://127.0.0.1:9900/callback?code=C&state=S")
    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(answers))
    client.authorization_url = lambda: (
        "https://accounts.spotify.com/authorize?x",
        "S",
        "V",
    )
    client.finish_login = AsyncMock()

    await prompter.prompt_and_login()

    assert all("Client ID" not in question for question in answers.questions)


@pytest.mark.login
async def test_the_browser_login_catches_the_code_itself(spotify_prompter, monkeypatch):
    prompter, config, client = spotify_prompter
    config.session.spotify.client_id = "saved"
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    opened = []
    monkeypatch.setattr(prompter_module, "_open_login_link", opened.append)
    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(Answers("1")))
    capture = AsyncMock(return_value="CAUGHT")
    monkeypatch.setattr(prompter_module, "capture_code", capture)
    client.authorization_url = lambda: (
        "https://accounts.spotify.com/authorize?x",
        "S",
        "V",
    )
    client.finish_login = AsyncMock()

    await prompter.prompt_and_login()

    assert opened == ["https://accounts.spotify.com/authorize?x"]
    capture.assert_awaited_once_with("http://127.0.0.1:9900/callback", "S")
    client.finish_login.assert_awaited_once_with("CAUGHT", "V")


@pytest.mark.login
@pytest.mark.parametrize(("screen", "default"), [(True, "1"), (False, "2")])
async def test_without_a_screen_the_other_device_is_the_default(
    spotify_prompter, monkeypatch, screen, default
):
    """A browser elsewhere can't reach the 127.0.0.1 Spotify sends it back to."""
    prompter, config, client = spotify_prompter
    config.session.spotify.client_id = "saved"
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(prompter_module, "has_screen", lambda: screen)
    defaults = []

    def ask(question, default=None, **_):
        defaults.append(default)
        return (
            "2"
            if question == "Choose"
            else "http://127.0.0.1:9900/callback?code=C&state=S"
        )

    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(ask))
    client.authorization_url = lambda: ("https://accounts.spotify.com/x", "S", "V")
    client.finish_login = AsyncMock()

    await prompter.prompt_and_login()

    assert defaults[0] == default
    client.finish_login.assert_awaited_once_with("C", "V")


@pytest.mark.login
async def test_a_failed_login_can_be_tried_again(spotify_prompter, monkeypatch):
    prompter, config, client = spotify_prompter
    config.session.spotify.client_id = "saved"
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(
        prompter_module.Prompt, "ask", staticmethod(Answers("2", "bad", "2", "good"))
    )
    monkeypatch.setattr(
        prompter_module.Confirm, "ask", staticmethod(lambda *a, **k: True)
    )
    client.authorization_url = lambda: (
        "https://accounts.spotify.com/authorize?x",
        "S",
        "V",
    )
    client.finish_login = AsyncMock()
    pasted = iter([SpotifyLoginError("That address has no login in it."), "GOOD-CODE"])

    def code_from(address, state):
        result = next(pasted)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(prompter_module, "code_from_redirect_url", code_from)

    await prompter.prompt_and_login()

    client.finish_login.assert_awaited_once_with("GOOD-CODE", "V")


@pytest.mark.login
async def test_giving_up_on_the_login_is_an_authentication_error(
    spotify_prompter, monkeypatch
):
    prompter, config, client = spotify_prompter
    config.session.spotify.client_id = "saved"
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(
        prompter_module.Prompt, "ask", staticmethod(Answers("2", "bad"))
    )
    monkeypatch.setattr(
        prompter_module.Confirm, "ask", staticmethod(lambda *a, **k: False)
    )
    client.authorization_url = lambda: (
        "https://accounts.spotify.com/authorize?x",
        "S",
        "V",
    )
    monkeypatch.setattr(
        prompter_module,
        "code_from_redirect_url",
        MagicMock(side_effect=SpotifyLoginError("no")),
    )

    with pytest.raises(AuthenticationError, match="cancelled"):
        await prompter.prompt_and_login()


@pytest.mark.login
def test_saving_copies_the_app_and_the_login_to_the_file(spotify_prompter, monkeypatch):
    prompter, config, _ = spotify_prompter
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    session = config.session.spotify
    session.client_id, session.access_token = "cid", "access"
    session.refresh_token, session.token_expiry = "refresh", "1900000000"

    prompter.save()

    saved = config.file.spotify
    assert (saved.client_id, saved.access_token) == ("cid", "access")
    assert (saved.refresh_token, saved.token_expiry) == ("refresh", "1900000000")
    assert config.file.modified
