"""Unit tests for the conditional download-store (free/purchased account) path.

A free Qobuz account (no streaming subscription) returns an empty
``credential.parameters`` on login. It cannot stream, but it can still download
albums it has *purchased*. These tests verify that:

* a subscriber (non-empty parameters) is NOT flagged download-only and keeps
  using ``intent=stream`` (no streaming regression),
* a free account (empty parameters) is flagged ``download_only`` without
  raising, and
* ``_request_file_url`` selects the matching ``intent`` in BOTH the signed
  request-signature preimage and the params dict.

All network calls are mocked; these run without Qobuz credentials.
"""

from unittest.mock import AsyncMock

import pytest

from streamrip.client.qobuz import FORMAT_IDS, QobuzClient, file_url_signature
from streamrip.config import Config


class _FakeSession:
    """Minimal stand-in for the aiohttp session used during login()."""

    def __init__(self):
        self.headers = {}

    async def close(self):
        pass


def _make_client() -> QobuzClient:
    config = Config.defaults()
    c = config.session.qobuz
    c.user_id = "13103092"
    c.auth_token = "fake-token"
    # Pre-seed app_id/secrets so login() skips the spoofer/network fetch.
    c.app_id = "123456789"
    c.secrets = ["fakesecret"]
    return QobuzClient(config)


def _login_resp(parameters):
    return {
        "user": {"credential": {"parameters": parameters}},
        "user_auth_token": "fake-uat",
    }


async def _login(monkeypatch, parameters) -> QobuzClient:
    client = _make_client()
    monkeypatch.setattr(
        "streamrip.client.client.new_session", lambda **_: _FakeSession()
    )
    monkeypatch.setattr(
        client,
        "_api_request",
        AsyncMock(return_value=(200, _login_resp(parameters))),
    )
    monkeypatch.setattr(
        client, "_get_valid_secret", AsyncMock(return_value="fakesecret")
    )
    await client.login()
    return client


@pytest.mark.login
@pytest.mark.parametrize(
    ("parameters", "download_only"),
    [
        # A subscriber keeps streaming: no regression.
        ({"lossy_streaming": True, "hires_streaming": True}, False),
        # A free account can't stream but can still download what it bought.
        ([], True),
    ],
)
async def test_login_flags_an_account_that_cannot_stream_as_download_only(
    monkeypatch, parameters, download_only
):
    """Empty credential.parameters means download only; neither case raises."""
    client = await _login(monkeypatch, parameters)

    assert client.download_only is download_only
    assert client.logged_in is True


@pytest.mark.parametrize(
    ("download_only", "intent"), [(True, "download"), (False, "stream")]
)
async def test_the_file_url_request_uses_the_matching_intent(
    monkeypatch, download_only, intent
):
    """The intent is the same in the params and in the signed preimage."""
    secret = "abc123secret"
    track_id = "19512574"
    quality = 3
    client = _make_client()
    client.download_only = download_only
    captured: dict = {}

    async def fake_api(epoint, params):
        captured["params"] = params
        return (200, {})

    monkeypatch.setattr(client, "_api_request", fake_api)
    await client._request_file_url(track_id, quality, secret)

    params = captured["params"]
    assert params["intent"] == intent
    assert params["request_sig"] == file_url_signature(
        FORMAT_IDS[quality - 1], intent, track_id, params["request_ts"], secret
    )


def test_file_url_signature_known_answer():
    """Pin the signature format Qobuz expects (known-answer test)."""
    assert (
        file_url_signature(27, "stream", "19512574", 1700000000.5, "secret")
        == "7e51324fbe3fb7a052f218264ed520f1"
    )
