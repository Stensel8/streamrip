"""The [spotify] section, and the prompter that fills it."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
import tomlkit

from streamrip.client import SpotifyClient
from streamrip.config import Config, ConfigData, set_user_defaults
from streamrip.exceptions import AuthenticationError
from streamrip.rip import prompter as prompter_module
from streamrip.rip.prompter import PROMPTERS, SpotifyPrompter, get_prompter
from streamrip.rip.spotify_login import SpotifyLoginError

# --- the config --------------------------------------------------------------


def test_the_defaults_are_what_the_template_says():
    spotify = Config.defaults().session.spotify

    assert spotify.client_id == ""
    assert spotify.redirect_uri == "http://127.0.0.1:9900/callback"
    assert spotify.audio_format == "m4a"
    assert spotify.audio_bitrate == 256
    assert spotify.match_videos is True
    assert (spotify.access_token, spotify.refresh_token, spotify.token_expiry) == (
        "",
    ) * 3


def test_the_audio_comes_in_one_quality():
    config = Config.defaults()

    assert config.session.get_source("spotify").quality == 0


def test_the_template_explains_the_app_and_the_premium_rule():
    text = open("streamrip/config.toml").read()
    section = text[text.index("[spotify]") : text.index("[database]")]

    assert "Premium" in section
    assert "https://developer.spotify.com/dashboard" in section
    assert "Web API" in section
    assert "127.0.0.1" in section


def test_a_config_from_before_spotify_still_loads(tmp_path):
    """An existing config has no [spotify]: it gets the template's, not an error."""
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    document = tomlkit.parse(path.read_text())
    del document["spotify"]
    document["qobuz"]["user_id"] = "123"
    path.write_text(tomlkit.dumps(document))
    assert "[spotify]" not in path.read_text()

    config = Config(str(path))

    assert config.session.qobuz.user_id == "123"
    assert config.session.spotify.audio_format == "m4a"
    assert config.file.spotify.client_id == ""


def test_the_section_is_written_when_the_config_is_saved(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    document = tomlkit.parse(path.read_text())
    del document["spotify"]
    path.write_text(tomlkit.dumps(document))

    with Config(str(path)) as config:
        config.file.spotify.client_id = "abc"
        config.file.spotify.refresh_token = "r"
        config.file.set_modified()

    saved = tomlkit.parse(path.read_text())
    assert saved["spotify"]["client_id"] == "abc"
    assert saved["spotify"]["refresh_token"] == "r"
    assert saved["spotify"]["audio_format"] == "m4a"
    # The rest of the file is as it was.
    assert saved["qobuz"]["quality"] == 4
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_a_config_that_has_the_section_is_left_as_it_is():
    toml = (
        open("streamrip/config.toml")
        .read()
        .replace('audio_format = "m4a"', 'audio_format = "mp3"')
    )

    assert ConfigData.from_toml(toml).spotify.audio_format == "mp3"


def test_options_missing_from_the_section_have_defaults():
    toml = open("streamrip/config.toml").read().replace("match_videos = true\n", "")

    assert ConfigData.from_toml(toml).spotify.match_videos is True


# --- the prompter ------------------------------------------------------------


@pytest.fixture
def spotify_prompter():
    config = Config.defaults()
    client = SpotifyClient(config)
    return SpotifyPrompter(config, client), config, client


def test_the_prompter_is_registered_for_spotify(spotify_prompter):
    _, config, client = spotify_prompter

    assert PROMPTERS["spotify"] is SpotifyPrompter
    assert isinstance(get_prompter(client, config), SpotifyPrompter)


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
