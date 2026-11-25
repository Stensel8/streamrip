"""Logins of the playlist tools: OAuth with PKCE, refreshing, and the tokens in config.toml."""

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import responses
from playlist_support import config_at, sent_form

from streamrip.config import Config
from streamrip.sync.auth import (
    ClientCredentials,
    ConfigTokenStore,
    OAuthClient,
    StoredToken,
    Token,
    pkce_pair,
)
from streamrip.sync.errors import LoginError, NetworkError, NotLoggedInError

TOKEN_URL = "https://auth.test/token"


class DemoOAuth(OAuthClient):
    name = "spotify"
    label = "Demo"
    authorize_endpoint = "https://auth.test/authorize"
    token_endpoint = TOKEN_URL
    scopes = ("read", "write")


def demo(secret: str = "") -> DemoOAuth:
    return DemoOAuth("cid", "http://127.0.0.1:8888/cb", secret)


def store_at(tmp_path) -> ConfigTokenStore:
    return ConfigTokenStore(config_at(tmp_path))


def test_the_pkce_challenge_is_the_sha256_of_the_verifier():
    verifier, challenge = pkce_pair()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_the_authorize_url_carries_pkce_state_and_scopes():
    query = parse_qs(urlsplit(demo().authorize_url("STATE", "CHAL")).query)
    assert query["state"] == ["STATE"]
    assert query["code_challenge"] == ["CHAL"]
    assert query["scope"] == ["read write"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8888/cb"]


def test_a_login_is_written_to_the_config_at_once(tmp_path):
    store = store_at(tmp_path)
    assert store.load("spotify") is None
    store.save("spotify", Token("A", "R", 1234.0, "user-library-read"))
    store.save("tidal", Token("TA", "TR", 99.0))
    again = Config(tmp_path / "config.toml")  # what another streamrip reads
    assert again.file.spotify.access_token == "A"
    # Tidal's official API has a login of its own, next to the download login.
    assert (again.file.tidal.api_access_token, again.file.tidal.access_token) == (
        "TA",
        "",
    )
    assert ConfigTokenStore(again).load("tidal") == Token("TA", "TR", 99.0)


def test_the_download_login_of_spotify_is_the_playlist_login(tmp_path):
    config = config_at(
        tmp_path,
        spotify={"access_token": "A", "refresh_token": "R", "token_expiry": "50"},
    )
    assert ConfigTokenStore(config).load("spotify") == Token("A", "R", 50.0)


@responses.activate
def test_exchange_sends_the_verifier_and_parses_the_token():
    responses.post(
        TOKEN_URL,
        json={"access_token": "A", "refresh_token": "R", "expires_in": 100},
    )
    token = demo().exchange("CODE", "VERIFIER")
    form = sent_form(responses.calls[0])
    assert form["code"] == ["CODE"]
    assert form["code_verifier"] == ["VERIFIER"]
    assert "client_secret" not in form
    assert (token.access_token, token.refresh_token) == ("A", "R")
    assert not token.expired()


@responses.activate
def test_refresh_keeps_the_old_refresh_token_when_none_is_returned():
    responses.post(TOKEN_URL, json={"access_token": "NEW", "expires_in": 100})
    fresh = demo("S").refresh(Token("OLD", "R", 0))
    assert (fresh.access_token, fresh.refresh_token) == ("NEW", "R")


@responses.activate
def test_client_credentials_use_basic_auth_and_are_cached():
    responses.post(TOKEN_URL, json={"access_token": "APP", "expires_in": 1000})
    provider = ClientCredentials(demo("S"))
    assert provider() == provider() == "APP"
    assert len(responses.calls) == 1
    basic = base64.b64encode(b"cid:S").decode()
    assert responses.calls[0].request.headers["Authorization"] == f"Basic {basic}"


@responses.activate
def test_token_endpoint_errors_become_login_errors():
    responses.post(
        TOKEN_URL,
        status=400,
        json={"error": "invalid_grant", "error_description": "Code expired"},
    )
    with pytest.raises(LoginError, match="Code expired"):
        demo().exchange("CODE", "V")


@responses.activate
@pytest.mark.parametrize(
    "answer",
    [{"body": "<html>Down for maintenance</html>"}, {"json": {"token_type": "Bearer"}}],
)
def test_a_token_answer_that_makes_no_sense_is_a_login_error(answer):
    responses.post(TOKEN_URL, **answer)
    with pytest.raises(LoginError, match="usable token"):
        demo().exchange("CODE", "V")


@responses.activate
def test_an_expired_token_is_refreshed_and_saved(tmp_path):
    store = store_at(tmp_path)
    provider = StoredToken(demo(), store)
    with pytest.raises(NotLoggedInError):
        provider()
    store.save("spotify", Token("old", "R", time.time() - 10))
    responses.post(TOKEN_URL, json={"access_token": "new", "expires_in": 100})
    assert provider() == "new"
    assert store.load("spotify").access_token == "new"
    assert provider() == "new"
    assert len(responses.calls) == 1


@responses.activate
def test_a_token_another_program_already_refreshed_is_not_refreshed_again(tmp_path):
    # Refresh tokens can be single-use: spending the same one twice kills the login.
    config_at(tmp_path)
    ours, theirs = Config(tmp_path / "config.toml"), Config(tmp_path / "config.toml")
    ConfigTokenStore(ours).save("spotify", Token("old", "R1", 0))
    ConfigTokenStore(theirs).reload("spotify")
    ConfigTokenStore(theirs).save("spotify", Token("fresh", "R2", time.time() + 3600))

    assert StoredToken(demo(), ConfigTokenStore(ours))() == "fresh"
    assert len(responses.calls) == 0


@responses.activate
def test_a_failed_refresh_asks_for_a_new_login(tmp_path):
    store = store_at(tmp_path)
    store.save("spotify", Token("old", "R", 0))
    responses.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    with pytest.raises(NotLoggedInError, match="streamrip login spotify"):
        StoredToken(demo(), store)()


@responses.activate
def test_a_login_server_in_trouble_is_not_an_expired_session(tmp_path):
    # "Log in again" would be the wrong advice for a 503.
    store = store_at(tmp_path)
    store.save("spotify", Token("old", "R", 0))
    responses.post(TOKEN_URL, status=503)
    with pytest.raises(NetworkError, match="HTTP 503"):
        StoredToken(demo(), store)()
    assert store.load("spotify") == Token("old", "R", 0)
