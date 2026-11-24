import pytest
from click.testing import CliRunner

from streamrip import verify as verify_module
from streamrip.client import DeezerClient, QobuzClient, TidalClient
from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import AuthenticationError
from streamrip.rip import accounts_cli
from streamrip.rip.cli import rip


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    return Config(path)


def log_in_to_qobuz(config):
    config.update_logins("qobuz", {"user_id": "1", "auth_token": "t"})


class FakeClient:
    """A download client whose login works, or fails with `error`."""

    plan = ("Studio, hi-res", 4)
    error: Exception | None = None

    def __init__(self, config):
        pass

    async def login(self):
        if self.error:
            raise self.error

    async def account_plan(self):
        return self.plan

    async def close(self):
        pass


async def test_nothing_is_tried_where_there_is_no_login(config):
    found = {r.id: r for r in await verify_module.verify(config)}
    assert found["qobuz"].state == verify_module.NOT_LOGGED_IN
    assert found["qobuz"].fix == "streamrip login qobuz"
    assert found["spotify"].state == verify_module.NEEDS_APP
    assert not any(r.state == verify_module.OK for r in found.values())


async def test_a_working_login_shows_its_plan(config, monkeypatch):
    log_in_to_qobuz(config)
    monkeypatch.setattr(verify_module, "QobuzClient", FakeClient)
    (found,) = await verify_module.verify(config, ("qobuz",))
    assert (found.state, found.detail, found.hint) == ("ok", "Studio, hi-res", "")


async def test_a_quality_the_plan_cannot_give_is_pointed_out(config, monkeypatch):
    log_in_to_qobuz(config)  # quality = 4 in the template
    monkeypatch.setattr(FakeClient, "plan", ("Sublime, lossless", 2))
    monkeypatch.setattr(verify_module, "QobuzClient", FakeClient)
    (found,) = await verify_module.verify(config, ("qobuz",))
    assert found.state == "ok"
    assert "set it to 2" in found.hint


async def test_a_login_the_service_refuses_says_how_to_fix_it(config, monkeypatch):
    log_in_to_qobuz(config)
    monkeypatch.setattr(FakeClient, "error", AuthenticationError("token expired"))
    monkeypatch.setattr(verify_module, "QobuzClient", FakeClient)
    (found,) = await verify_module.verify(config, ("qobuz",))
    assert (found.state, found.detail) == ("failed", "token expired")
    assert found.fixable


async def test_a_service_that_does_not_answer_is_no_login_problem(config, monkeypatch):
    log_in_to_qobuz(config)

    class Silent(FakeClient):
        async def login(self):
            import asyncio

            await asyncio.sleep(5)

    monkeypatch.setattr(verify_module, "TIMEOUT", 0.05)
    monkeypatch.setattr(verify_module, "QobuzClient", Silent)
    (found,) = await verify_module.verify(config, ("qobuz",))
    assert found.state == "failed"
    assert "no answer" in found.detail
    assert not found.fixable  # logging in again would not help


async def test_a_library_login_is_asked_for_its_account(config, monkeypatch):
    config.update_logins(
        "spotify", {"client_id": "app", "access_token": "a", "refresh_token": "r"}
    )

    class Provider:
        def account(self):
            return "Premium, 12 liked songs"

    class Services:
        def __init__(self, _config):
            pass

        def provider(self, name):
            assert name == "spotify"
            return Provider()

    monkeypatch.setattr(verify_module, "Services", Services)
    (found,) = await verify_module.verify(config, ("spotify",))
    assert (found.state, found.detail) == ("ok", "Premium, 12 liked songs")


# --- what the clients say about the account -----------------------------------


async def test_qobuz_says_what_its_credential_allows():
    client = QobuzClient(Config.defaults())
    client._credential = {"label": "Studio", "parameters": {"hires_streaming": True}}
    assert await client.account_plan() == ("Studio, hi-res", 4)
    client._credential = {"parameters": {"lossless_streaming": True}}
    assert await client.account_plan() == ("lossless", 2)
    client._credential = {"parameters": {}}
    assert (await client.account_plan())[1] is None  # purchases only


async def test_deezer_says_what_the_licence_allows():
    client = DeezerClient(Config.defaults())
    client.current_user = {"can_stream_hq": True, "can_stream_lossless": True}
    assert await client.account_plan() == ("lossless (FLAC)", 2)
    client.current_user = {"can_stream_hq": True, "can_stream_lossless": False}
    assert (await client.account_plan())[1] == 1


async def test_tidal_says_its_best_quality_and_keeps_quiet_when_it_cannot(monkeypatch):
    client = TidalClient(Config.defaults())

    async def hi_res(*_a, **_k):
        return {"subscription": {"type": "HIFI"}, "highestSoundQuality": "HI_RES"}

    monkeypatch.setattr(client, "_api_request", hi_res)
    assert await client.account_plan() == ("Hifi, up to hi-res", 3)

    async def unknown(*_a, **_k):
        return {"highestSoundQuality": "SOMETHING_NEW"}

    monkeypatch.setattr(client, "_api_request", unknown)
    assert await client.account_plan() == ("", None)


# --- the command ----------------------------------------------------------------


def run_cli(tmp_path, *args):
    result = CliRunner().invoke(
        rip, ["--config-path", str(tmp_path / "config.toml"), *args]
    )
    return result.exit_code, result.output


def test_verify_exits_1_for_a_saved_login_that_fails(tmp_path, monkeypatch):
    set_user_defaults(tmp_path / "config.toml")
    log_in_to_qobuz(Config(tmp_path / "config.toml"))
    monkeypatch.setattr(FakeClient, "error", AuthenticationError("token expired"))
    monkeypatch.setattr(verify_module, "QobuzClient", FakeClient)
    code, output = run_cli(tmp_path, "verify", "qobuz")
    assert code == 1
    assert "token expired" in output
    assert "→ streamrip login qobuz" in output


def test_verify_without_logins_is_no_failure_and_says_what_to_do(tmp_path):
    set_user_defaults(tmp_path / "config.toml")
    code, output = run_cli(tmp_path, "verify")
    assert code == 0
    assert "not logged in" in output
    assert "streamrip verify --fix" in output
    assert "soundcloud" in output


def test_verify_fix_logs_in_to_what_needs_it_and_checks_again(tmp_path, monkeypatch):
    set_user_defaults(tmp_path / "config.toml")
    asked = []

    def log_in(config, service):
        asked.append(service)
        log_in_to_qobuz(config)

    monkeypatch.setattr(accounts_cli, "_log_in_again", log_in)
    monkeypatch.setattr(verify_module, "QobuzClient", FakeClient)
    code, output = run_cli(tmp_path, "verify", "qobuz", "--fix")
    assert code == 0
    assert asked == ["qobuz"]
    assert "Studio, hi-res" in output  # the second round


async def test_soundcloud_without_a_login_is_a_guest_and_no_problem(config):
    (found,) = await verify_module.verify(config, ("soundcloud",))
    assert found.state == verify_module.GUEST
    assert found.fix == "streamrip login soundcloud"
    assert not found.fixable  # nobody is nagged to log in


async def test_a_saved_soundcloud_login_that_is_not_taken_is_reported(
    config, monkeypatch
):
    config.update_logins("soundcloud", {"oauth_token": "stale"})

    class Refused(FakeClient):
        guest_reason = "SoundCloud did not take the saved login (HTTP 401)"

    monkeypatch.setattr(verify_module, "SoundcloudClient", Refused)
    (found,) = await verify_module.verify(config, ("soundcloud",))
    assert (found.state, found.fixable) == ("failed", True)
    assert "HTTP 401" in found.detail
