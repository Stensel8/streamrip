import asyncio
import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from streamrip import verify as verify_module
from streamrip.accounts import Accounts
from streamrip.client import QobuzClient
from streamrip.client import spotify as spotify_module
from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import AuthenticationError
from streamrip.rip import accounts_cli
from streamrip.rip.cli import rip
from streamrip.sync.errors import ApiError


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


def log_in_to_spotify(config, monkeypatch, *, reply=None, error=None):
    """A saved Spotify login whose API answers `reply`, or fails with `error`."""
    config.update_logins(
        "spotify",
        {
            "client_id": "app",
            "access_token": "a",
            "refresh_token": "r",
            "token_expiry": str(int(time.time()) + 3600),
        },
    )

    def request(*_args, **_kwargs):
        if error:
            raise error
        return reply

    monkeypatch.setattr(
        verify_module, "ApiClient", lambda *a, **k: SimpleNamespace(request=request)
    )


@pytest.mark.parametrize(
    ("plan", "hint"),
    [(("Studio, hi-res", 4), ""), (("Sublime, lossless", 2), "set it to 2")],
    ids=["fits", "quality the plan cannot give"],
)
async def test_a_working_login_shows_its_plan(config, monkeypatch, plan, hint):
    log_in_to_qobuz(config)  # quality = 4 in the template
    monkeypatch.setattr(FakeClient, "plan", plan)
    monkeypatch.setattr(verify_module, "QobuzClient", FakeClient)
    (found,) = await verify_module.verify(config, ("qobuz",))
    assert (found.state, found.detail) == ("ok", plan[0])
    assert hint in found.hint


async def test_the_quality_hint_compares_with_what_was_asked(config, monkeypatch):
    # The login lowers the session's quality to the plan; the hint must still
    # compare with what the config asks for.
    log_in_to_qobuz(config)
    config.session.qobuz.quality = 4

    class Lowers(FakeClient):
        plan = ("lossless", 2)

        async def login(self):
            self.requested_quality = config.session.qobuz.quality
            config.session.qobuz.quality = 2

    monkeypatch.setattr(verify_module, "QobuzClient", Lowers)
    (found,) = await verify_module.verify(config, ("qobuz",))
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
            await asyncio.sleep(5)

    monkeypatch.setattr(verify_module, "TIMEOUT", 0.05)
    monkeypatch.setattr(verify_module, "QobuzClient", Silent)
    (found,) = await verify_module.verify(config, ("qobuz",))
    assert found.state == "failed"
    assert not found.fixable  # logging in again would not help


async def test_one_service_failing_does_not_end_the_check(config, monkeypatch):
    log_in_to_qobuz(config)
    config.update_logins("deezer", {"arl": "cookie"})

    class Broken(FakeClient):
        async def login(self):
            raise KeyError("credential")

    monkeypatch.setattr(verify_module, "QobuzClient", Broken)
    monkeypatch.setattr(verify_module, "DeezerClient", FakeClient)
    found = {r.id: r for r in await verify_module.verify(config, ("qobuz", "deezer"))}
    assert found["qobuz"].state == "failed"
    assert found["deezer"].state == "ok"


async def test_soundcloud_without_a_login_is_a_guest_and_no_problem(config):
    (found,) = await verify_module.verify(config, ("soundcloud",))
    assert found.state == verify_module.GUEST
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


@pytest.mark.parametrize(
    ("source", "plan", "best", "described"),
    [
        ("qobuz", "streaming-studio, hi-res", 4, ("Paid", "FLAC up to 24-bit/192 kHz")),
        ("qobuz", "downloads of purchases only", None, ("Purchases only", "")),
        ("tidal", "Premium, up to hi-res", 3, ("Paid", "FLAC up to 24-bit/192 kHz")),
        ("tidal", "Free, up to AAC 96", 0, ("Free", "AAC")),
        ("deezer", "lossless (FLAC)", 2, ("Paid", "FLAC 16-bit/44.1 kHz")),
        ("deezer", "free (MP3 128)", 0, ("Free", "MP3 128")),
        ("soundcloud", "sten", None, ("", "AAC or MP3")),
    ],
)
def test_a_plan_is_described_as_free_or_paid_with_the_files_it_gives(
    source, plan, best, described
):
    assert verify_module.describe(source, plan, best) == described


async def test_spotify_says_who_you_are(config, monkeypatch):
    reply = {"product": "premium", "display_name": "Sten"}
    log_in_to_spotify(config, monkeypatch, reply=reply)
    (found,) = await verify_module.verify(config, ("spotify",))
    assert (found.state, found.detail) == ("ok", "Premium, Sten")
    # A token refresh on the way is kept, not lost.
    assert spotify_module.load_login(config).refresh_token == "r"


async def test_a_rate_limited_spotify_is_still_a_working_login(config, monkeypatch):
    # A 429 is on the account, not on the login.
    error = ApiError(429, "API rate limit exceeded")
    log_in_to_spotify(config, monkeypatch, error=error)
    (found,) = await verify_module.verify(config, ("spotify",))
    assert (found.state, found.detail) == ("ok", "the login is fine")
    assert "rate-limiting" in found.hint


async def test_spotify_without_an_app_of_ones_own_says_to_make_one(config):
    # A login left over from the pair link, which had no app of its own.
    config.update_logins("spotify", {"access_token": "a", "refresh_token": "r"})
    (found,) = await verify_module.verify(config, ("spotify",))
    assert found.state == "needs an app"


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (ApiError(401, "Invalid token"), "Invalid token"),
        (
            ApiError(403, "requires premium for this operation"),
            "Spotify only lets apps whose owner has a Premium subscription",
        ),
    ],
    ids=["refused", "premium required"],
)
async def test_a_login_spotify_refuses_says_how_to_fix_it(
    config, monkeypatch, error, detail
):
    log_in_to_spotify(config, monkeypatch, error=error)
    (found,) = await verify_module.verify(config, ("spotify",))
    assert found.state == "failed"
    assert detail in found.detail
    assert found.fix == "streamrip login spotify"


# --- what the clients say about the account -----------------------------------


async def test_qobuz_says_what_its_credential_allows():
    client = QobuzClient(Config.defaults())
    client._credential = {"label": "Studio", "parameters": {"hires_streaming": True}}
    assert await client.account_plan() == ("Studio, hi-res", 4)
    client._credential = {"parameters": {}}
    assert (await client.account_plan())[1] is None  # purchases only


@pytest.mark.parametrize(
    ("credential", "fitted"),
    [
        ({"lossless_streaming": True}, 2),  # the plan is lower than the setting
        ({"hires_streaming": True}, 4),
    ],
    ids=["lowered", "within the plan"],
)
async def test_a_quality_above_the_plan_is_lowered_for_the_run(credential, fitted):
    config = Config.defaults()
    qobuz = QobuzClient(config)
    qobuz._credential = {"parameters": credential}
    config.session.qobuz.quality = 4

    await qobuz._fit_quality()

    assert config.session.qobuz.quality == fitted
    assert config.file.qobuz.quality == 4  # the setting itself stays


# --- the command --------------------------------------------------------------


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
    assert "streamrip login qobuz" in output


def test_verify_without_logins_is_no_failure_and_says_what_to_do(tmp_path):
    set_user_defaults(tmp_path / "config.toml")
    code, output = run_cli(tmp_path, "verify")
    assert code == 0
    assert "not logged in" in output
    assert "streamrip verify --fix" in output


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


def test_a_login_that_ends_without_a_login_keeps_the_old_one(tmp_path, monkeypatch):
    set_user_defaults(tmp_path / "config.toml")
    log_in_to_qobuz(Config(tmp_path / "config.toml"))

    async def stopped(_config, _source, method=None):
        pass  # `coro` ends a Ctrl-C quietly, like this

    monkeypatch.setattr(accounts_cli, "_log_in", stopped)
    _code, output = run_cli(tmp_path, "login", "qobuz")
    assert "Logged in" not in output
    assert Accounts(Config(tmp_path / "config.toml")).logged_in("qobuz")
