"""The shared Spotify token service: one login for the downloads, the playlist
tools and the web alike. A refresh that rotates the refresh token is saved at
once, and the newest pair is the one that is kept: a refresh token Spotify
rotates is spent once, by whoever made it last."""

import time
from types import SimpleNamespace

import pytest
import requests
import responses
from playlist_support import config_at

from streamrip import spotify_auth
from streamrip.spotify_auth import (
    DEFAULT_CLIENT_ID,
    AuthServerDownError,
    DeviceLoginRefusedError,
    LoginRefusedError,
    NotLoggedInError,
    Token,
)


class FakeServer:
    """Answers with canned (status, payload) pairs, and keeps the forms it got.

    A `side_effect` is told of each form before the answer is given: how another
    program saves a rotated login while a refresh is in flight."""

    def __init__(self, *answers, side_effect=None):
        self.answers = list(answers) or [(200, {})]
        self.side_effect = side_effect
        self.forms = []

    def post(self, url, data=None, timeout=None):
        self.forms.append({"url": url, **(data or {})})
        if self.side_effect is not None:
            self.side_effect(self.forms[-1])
        status, payload = (
            self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        )
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(status_code=status, json=lambda: payload)


def saved(tmp_path, **values) -> None:
    """A login in [spotify]: a valid token unless `values` says otherwise."""
    config_at(
        tmp_path,
        spotify={
            "access_token": "A",
            "refresh_token": "R",
            "token_expiry": str(int(time.time()) + 3600),
            **values,
        },
    )


def kept(tmp_path):
    """What the next streamrip reads of the login."""
    return config_at(tmp_path).session.spotify


# --- refreshing ------------------------------------------------------------


def test_a_refresh_is_saved_at_once_with_its_rotated_token(tmp_path):
    # The failure of 2026-11-24: a refresh saved only at the end of a run left
    # the old refresh token in the file, and the next refresh spent it again.
    saved(tmp_path, token_expiry="1")
    fake = FakeServer(
        (200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 3600})
    )
    token = spotify_auth.refresh(config_at(tmp_path), fake)
    assert (token.access_token, token.refresh_token) == ("A2", "R2")
    assert (kept(tmp_path).access_token, kept(tmp_path).refresh_token) == ("A2", "R2")
    assert [f["refresh_token"] for f in fake.forms] == ["R"]


def test_a_refresh_outrun_by_another_program_takes_its_pair(tmp_path):
    saved(tmp_path, token_expiry="1")
    config = config_at(tmp_path)

    def another_program(_form):
        # Rotates the login while this refresh is in flight, and saves it.
        config.update_logins(
            "spotify",
            {
                "access_token": "A3",
                "refresh_token": "R3",
                "token_expiry": str(int(time.time()) + 3600),
            },
        )

    fake = FakeServer(
        (400, {"error": "invalid_grant", "error_description": "Refresh token revoked"}),
        side_effect=another_program,
    )
    token = spotify_auth.refresh(config_at(tmp_path), fake)
    assert (token.access_token, token.refresh_token) == ("A3", "R3")


def test_a_token_another_login_already_rotated_is_used_not_refreshed(tmp_path):
    saved(tmp_path, token_expiry="1")
    fake = FakeServer(
        (200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 3600})
    )
    spotify_auth.refresh(config_at(tmp_path), fake)
    # Another login in this process, holding the token that is now stale:
    token = spotify_auth.refresh(config_at(tmp_path), fake, stale="A")
    assert (token.access_token, token.refresh_token) == ("A2", "R2")
    assert len(fake.forms) == 1  # the rotated refresh token is not spent again


def test_a_refresh_spotify_refuses_is_a_login_error_and_keeps_the_login(tmp_path):
    saved(tmp_path, token_expiry="1")
    fake = FakeServer(
        (400, {"error": "invalid_grant", "error_description": "Refresh token revoked"})
    )
    with pytest.raises(LoginRefusedError, match="Refresh token revoked"):
        spotify_auth.refresh(config_at(tmp_path), fake)
    assert (kept(tmp_path).access_token, kept(tmp_path).refresh_token) == ("A", "R")


def test_a_refresh_without_a_login_is_a_not_logged_in_error(tmp_path):
    with pytest.raises(NotLoggedInError, match="Not logged in"):
        spotify_auth.refresh(config_at(tmp_path))


def test_a_login_server_in_trouble_is_not_a_refused_login(tmp_path):
    saved(tmp_path, token_expiry="1")
    with pytest.raises(AuthServerDownError, match="trouble"):
        spotify_auth.refresh(config_at(tmp_path), FakeServer((503, None)))
    with pytest.raises(AuthServerDownError, match="reach"):
        spotify_auth.refresh(
            config_at(tmp_path), FakeServer((0, requests.ConnectionError("no route")))
        )
    assert kept(tmp_path).refresh_token == "R"


def test_a_token_that_still_lasts_is_used_as_it_is(tmp_path):
    saved(tmp_path)
    fake = FakeServer()
    assert spotify_auth.token(config_at(tmp_path), fake).access_token == "A"
    assert fake.forms == []


# --- which client id a login belongs to -------------------------------------


def test_the_refresh_uses_the_client_id_the_login_belongs_to(tmp_path):
    # A login without an app of one's own: the method is kept with it.
    saved(tmp_path, token_expiry="1")
    fake = FakeServer((200, {"access_token": "A2", "expires_in": 3600}))
    spotify_auth.refresh(config_at(tmp_path), fake)
    assert fake.forms[0]["client_id"] == DEFAULT_CLIENT_ID
    assert kept(tmp_path).auth_method == "device"
    # An app login: the user's app.
    saved(tmp_path, client_id="MYAPP", auth_method="app", token_expiry="1")
    fake = FakeServer((200, {"access_token": "B2", "expires_in": 3600}))
    spotify_auth.refresh(config_at(tmp_path), fake)
    assert fake.forms[0]["client_id"] == "MYAPP"
    assert kept(tmp_path).auth_method == "app"


def test_how_a_login_was_made_is_inferred_for_the_old_ones(tmp_path):
    assert spotify_auth.auth_method(config_at(tmp_path)) == "device"
    config_at(tmp_path, spotify={"client_id": "MYAPP"})
    assert spotify_auth.auth_method(config_at(tmp_path)) == "app"
    # A saved method always wins over the client id.
    config_at(tmp_path, spotify={"client_id": "MYAPP", "auth_method": "device"})
    assert spotify_auth.auth_method(config_at(tmp_path)) == "device"


def test_a_saved_login_keeps_the_app_it_was_made_with(tmp_path):
    saved(tmp_path, client_id="MYAPP")
    spotify_auth.save(config_at(tmp_path), Token("A2", "R2", time.time() + 3600), "app")
    c = kept(tmp_path)
    assert (c.access_token, c.refresh_token, c.auth_method, c.client_id) == (
        "A2",
        "R2",
        "app",
        "MYAPP",
    )


def test_a_device_login_says_so_and_keeps_no_client_id_of_its_own(tmp_path):
    saved(tmp_path)
    spotify_auth.save(
        config_at(tmp_path), Token("A2", "R2", time.time() + 3600), "device"
    )
    c = kept(tmp_path)
    assert (c.auth_method, c.client_id) == ("device", "")


# --- the device login: a pair link, approved anywhere ------------------------


def test_requesting_a_pair_link_asks_for_spotify_s_own_client(tmp_path):
    fake = FakeServer(
        (
            200,
            {
                "device_code": "DC",
                "user_code": "WYGVFF",
                "verification_uri": "https://spotify.com/pair",
                "verification_uri_complete": "https://spotify.com/pair?code=WYGVFF",
                "expires_in": 3599,
                "interval": 5,
            },
        )
    )
    pair = spotify_auth.request_device_code(config_at(tmp_path), fake)
    assert (pair.device_code, pair.user_code) == ("DC", "WYGVFF")
    assert pair.verification_uri_complete == "https://spotify.com/pair?code=WYGVFF"
    assert fake.forms[0]["url"] == spotify_auth.DEVICE_AUTHORIZE_URL
    assert fake.forms[0]["client_id"] == DEFAULT_CLIENT_ID


def test_polling_takes_the_token_when_it_is_approved():
    sleeps = []
    fake = FakeServer(
        (400, {"error": "authorization_pending"}),
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": "A", "refresh_token": "R", "expires_in": 3600}),
    )
    token = spotify_auth.poll_for_token(
        "DC", None, fake, expires_in=60, interval=5, sleep=sleeps.append
    )
    assert (token.access_token, token.refresh_token) == ("A", "R")
    assert sleeps == [5, 5]
    assert [f["device_code"] for f in fake.forms] == ["DC"] * 3
    assert all(f["grant_type"] == spotify_auth.DEVICE_GRANT for f in fake.forms)


def test_polling_slows_down_when_spotify_asks():
    sleeps = []
    fake = FakeServer(
        (400, {"error": "slow_down"}),
        (200, {"access_token": "A", "expires_in": 3600}),
    )
    token = spotify_auth.poll_for_token(
        "DC", None, fake, expires_in=60, interval=5, sleep=sleeps.append
    )
    assert token.access_token == "A"
    assert sleeps == [7]  # the interval Spotify set, plus its two seconds


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ((400, {"error": "expired_token"}), "expired_token"),
        (
            (400, {"error": "access_denied", "error_description": "Spotify says no"}),
            "Spotify says no",
        ),
    ],
    ids=["expired", "denied"],
)
def test_a_device_login_spotify_ends_is_final(answer, message):
    with pytest.raises(DeviceLoginRefusedError, match=message):
        spotify_auth.poll_for_token(
            "DC",
            None,
            FakeServer(answer),
            expires_in=60,
            interval=1,
            sleep=lambda _: None,
        )


def test_a_wait_that_is_given_up_on_is_a_refusal():
    fake = FakeServer((400, {"error": "authorization_pending"}))
    with pytest.raises(DeviceLoginRefusedError, match="cancelled"):
        spotify_auth.poll_for_token(
            "DC",
            None,
            fake,
            expires_in=60,
            interval=1,
            stop=lambda: True,
            sleep=lambda _: None,
        )
    assert fake.forms == []  # it did not even ask


def test_a_pair_link_that_outlives_its_deadline_is_over():
    fake = FakeServer((400, {"error": "authorization_pending"}))

    class Clock:
        now = 0.0

        def __call__(self, seconds):
            Clock.now += seconds

    with pytest.raises(DeviceLoginRefusedError, match="expired"):
        spotify_auth.poll_for_token(
            "DC", None, fake, expires_in=10, interval=5, sleep=Clock()
        )
    assert fake.forms  # it kept polling until the deadline, not past it
    assert Clock.now >= 10


# --- the app login: the code the browser brings back -------------------------


def test_exchanging_the_code_of_an_app_login(tmp_path):
    saved(tmp_path, client_id="MYAPP")
    fake = FakeServer(
        (
            200,
            {
                "access_token": "A",
                "refresh_token": "R",
                "expires_in": 3600,
                "scope": "s",
            },
        )
    )
    token = spotify_auth.exchange(
        "C", "V", "http://127.0.0.1:9900/callback", "MYAPP", session=fake
    )
    assert (token.access_token, token.refresh_token, token.scope) == ("A", "R", "s")
    form = fake.forms[0]
    assert form["url"] == spotify_auth.TOKEN_URL
    assert form["grant_type"] == "authorization_code"
    assert (form["code"], form["code_verifier"], form["client_id"]) == (
        "C",
        "V",
        "MYAPP",
    )


def test_an_exchange_spotify_refuses_is_a_login_error():
    fake = FakeServer(
        (400, {"error": "invalid_grant", "error_description": "Code expired"})
    )
    with pytest.raises(LoginRefusedError, match="Code expired"):
        spotify_auth.exchange(
            "C", "V", "http://127.0.0.1:9900/callback", "cid", session=fake
        )


# --- the playlist tools refresh through the same service ---------------------


@responses.activate
def test_the_playlist_tools_refresh_the_shared_login_once(tmp_path):
    """One login, two tools: the playlist tools' refresh is the one the
    downloads make, so a rotated refresh token is spent once."""
    from streamrip.sync.auth import ConfigTokenStore, StoredToken
    from streamrip.sync.providers.spotify import SpotifyOAuth

    saved(tmp_path, client_id="MYAPP", token_expiry=str(int(time.time()) + 1000))
    store = ConfigTokenStore(config_at(tmp_path))

    def oauth():
        return SpotifyOAuth("MYAPP", "http://127.0.0.1:9900/callback")

    first, second = StoredToken(oauth(), store), StoredToken(oauth(), store)
    assert first() == second() == "A"
    responses.post(
        spotify_auth.TOKEN_URL,
        json={"access_token": "A2", "refresh_token": "R2", "expires_in": 100},
    )
    assert first(force_refresh=True) == "A2"  # this one refreshes
    assert second(force_refresh=True) == "A2"  # takes the pair first saved
    assert len(responses.calls) == 1
    saved_token = ConfigTokenStore(config_at(tmp_path)).load("spotify")
    assert saved_token is not None
    assert saved_token.refresh_token == "R2"


# --- the command line: the pair link is the way, --configure the other ------


@pytest.fixture
def device_login(monkeypatch):
    """A device login that goes through: a pair link, approved at once."""
    from streamrip.rip import accounts_cli

    pair = spotify_auth.DeviceCode(
        "DC",
        "ABC123",
        "https://spotify.com/pair",
        "https://spotify.com/pair?code=ABC123",
        3599,
        5,
    )
    token = Token("A", "R", time.time() + 3600)
    monkeypatch.setattr(
        accounts_cli.spotify_auth, "request_device_code", lambda config: pair
    )
    monkeypatch.setattr(
        accounts_cli.spotify_auth, "poll_for_token", lambda *a, **k: token
    )
    monkeypatch.setattr(
        accounts_cli,
        "spotify_device_login",
        lambda config: accounts_cli.spotify_auth.save(config, token, "device"),
    )
    return accounts_cli


def test_the_command_line_logs_spotify_in_with_a_pair_link(tmp_path, device_login):
    saved(tmp_path, client_id="MYAPP")
    config = config_at(tmp_path)

    device_login._log_in_again(config, "spotify")

    kept = config_at(tmp_path).session.spotify
    assert (kept.access_token, kept.refresh_token, kept.auth_method) == (
        "A",
        "R",
        "device",
    )
    # The app is kept: a login is how it was made, and the app is one's own.
    assert kept.client_id == "MYAPP"


def test_a_device_login_that_fails_keeps_the_old_login(
    tmp_path, device_login, monkeypatch
):
    saved(tmp_path, token_expiry=str(int(time.time()) + 3600))
    config = config_at(tmp_path)
    monkeypatch.setattr(
        device_login,
        "spotify_device_login",
        lambda config: (_ for _ in ()).throw(
            spotify_auth.AuthServerDownError("the login server is having trouble")
        ),
    )

    with pytest.raises(Exception, match="oAuth2 failed"):
        device_login._log_in_again(config, "spotify")

    kept = config_at(tmp_path).session.spotify
    assert kept.refresh_token == "R"  # the working login is still there
