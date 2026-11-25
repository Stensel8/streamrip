from streamrip.accounts import LOGIN_IDS, Accounts
from streamrip.config import Config, set_user_defaults


def accounts_at(tmp_path) -> Accounts:
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    return Accounts(Config(path))


def test_nothing_is_logged_in_on_a_first_run(tmp_path):
    statuses = {s.login.id: s for s in accounts_at(tmp_path).all()}
    assert tuple(statuses) == LOGIN_IDS
    assert not any(s.logged_in for s in statuses.values())


def test_a_login_needs_all_its_parts(tmp_path):
    accounts = accounts_at(tmp_path)
    accounts.config.update_logins("qobuz", {"user_id": "1"})
    assert not accounts.logged_in("qobuz")  # no token yet
    accounts.config.update_logins("qobuz", {"auth_token": "t"})
    assert accounts.logged_in("qobuz")


def test_a_logout_keeps_the_app_and_can_be_undone(tmp_path):
    accounts = accounts_at(tmp_path)
    accounts.config.update_logins(
        "spotify", {"client_id": "app", "access_token": "a", "refresh_token": "r"}
    )
    held = accounts.logout("spotify")
    assert not accounts.logged_in("spotify")
    saved = Config(tmp_path / "config.toml").file.spotify
    assert (saved.client_id, saved.refresh_token) == ("app", "")

    accounts.restore("spotify", held)
    assert Config(tmp_path / "config.toml").file.spotify.refresh_token == "r"


def test_tidals_two_logins_are_apart(tmp_path):
    accounts = accounts_at(tmp_path)
    accounts.config.update_logins(
        "tidal", {"access_token": "a", "api_client_id": "app", "api_refresh_token": "r"}
    )
    assert accounts.logged_in("tidal")
    accounts.logout("tidal")
    assert not accounts.logged_in("tidal")
    assert accounts.logged_in("tidal-library")
