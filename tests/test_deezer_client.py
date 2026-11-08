"""Unit tests for Deezer client behaviour that needs no network or ARL."""

import copy
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from streamrip.client.deezer import (
    DeezerClient,
    DeezerError,
    WrongGeolocationError,
    WrongLicenseError,
)
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError

TRACK_INFO = {
    "FILESIZE_FLAC": 30_000_000,
    "FILESIZE_MP3_320": 9_000_000,
    "FILESIZE_MP3_128": 4_000_000,
    "TRACK_TOKEN": "token",
}


def _client(lower_quality=True, **account) -> DeezerClient:
    config = Config.defaults()
    config.session.deezer.lower_quality_if_not_available = lower_quality
    client = DeezerClient(config)
    client.client = AsyncMock()
    client.client.current_user = {"license_token": "x", **account}
    client.client.gw.get_track.return_value = dict(TRACK_INFO)
    client.client.get_track_url.return_value = "https://cdn.example/track"
    client.session = Mock()
    return client


async def test_free_account_is_clamped_to_mp3_128():
    """Upstream #1015: quality 1 on a free account used to claim HiFi was needed."""
    client = _client(can_stream_hq=False, can_stream_lossless=False)
    dl = await client.get_downloadable("1", quality=1)
    assert dl.quality == 0
    client.client.get_track_url.assert_called_once_with("token", "MP3_128")


@pytest.mark.parametrize(
    ("can_stream_lossless", "requested", "quality", "extension"),
    [
        (False, 2, 1, "mp3"),  # a premium account is clamped to MP3 320
        (True, 4, 2, "flac"),  # `streamrip --quality 4` used to raise IndexError
    ],
)
async def test_the_quality_is_clamped_to_what_the_account_can_stream(
    can_stream_lossless, requested, quality, extension
):
    client = _client(can_stream_hq=True, can_stream_lossless=can_stream_lossless)
    dl = await client.get_downloadable("1", quality=requested)
    assert dl.quality == quality
    assert dl.extension == extension


async def test_clamp_can_be_disabled():
    client = _client(lower_quality=False, can_stream_hq=False)
    with pytest.raises(NonStreamableError, match="subscription"):
        await client.get_downloadable("1", quality=1)


async def test_playlist_falls_back_to_gw_api():
    client = _client()
    client.client.api.get_playlist.side_effect = Exception("PermissionException")
    client.client.gw.get_playlist_page.return_value = {"DATA": {"TITLE": "Mine"}}
    client.client.gw.get_playlist_tracks.return_value = [
        {"SNG_ID": "11"},
        {"SNG_ID": "12"},
    ]
    resp = await client._get_playlist("42")
    assert resp["title"] == "Mine"
    assert [t["id"] for t in resp["tracks"]] == ["11", "12"]


async def test_redirected_album_id_is_followed():
    """Upstream #893: /album/<old id>/tracks has no data, the new id does."""
    client = _client()
    client.client.api.get_album.return_value = {"id": 723513301, "title": "New"}

    def tracks(album_id):
        if str(album_id) == "997825":
            raise Exception("DataException: no data")
        return {"data": [{"id": 1, "disk_number": 1}]}

    client.client.api.get_album_tracks.side_effect = tracks
    resp = await client._get_album("997825")
    assert resp["track_total"] == 1


async def test_loved_albums_are_a_label_of_the_profile():
    client = _client()
    client.client.gw.get_user_albums.return_value = [{"id": 11}, {"id": "12"}]
    resp = await client.get_metadata("favorites:99", "label")
    assert resp["albums"] == [{"id": "11"}, {"id": "12"}]
    assert client.client.gw.get_user_albums.call_args.args == ("99",)


async def test_a_playlist_id_may_be_a_number():
    client = _client()
    client.client.api.get_playlist.return_value = {"title": "P", "nb_tracks": 0}
    client.client.api.get_playlist_tracks.return_value = {"data": []}
    resp = await client.get_metadata(123, "playlist")
    assert resp["title"] == "P"


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def json(self, content_type=None):
        return self._body


class _FakeSession:
    """Answers each request with the next of `replies`, and keeps them."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.cookie_jar = Mock()

    def request(self, method, url, **kwargs):
        # A copy: aiohttp sends the body as it is now, whatever changes later.
        self.requests.append((method, url, copy.deepcopy(kwargs)))
        reply = self.replies.pop(0)
        return (
            _FakeResponse(*reply) if isinstance(reply, tuple) else _FakeResponse(reply)
        )


def _dz(*replies) -> DeezerClient:
    client = DeezerClient(Config.defaults())
    client.session = _FakeSession(*replies)
    return client


USER = {
    "checkForm": "csrf-1",
    "USER": {
        "USER_ID": 7,
        "OPTIONS": {
            "license_token": "lic",
            "web_hq": True,
            "mobile_hq": False,
            "web_lossless": False,
            "mobile_lossless": True,
            "license_country": "NL",
        },
        "SETTING": {"global": {"language": "nl"}},
    },
}


async def test_arl_login_reads_the_licence_and_language():
    client = _dz({"error": [], "results": USER})
    assert await client.client.login_via_arl(" the-arl ")
    assert client.client.current_user == {
        "id": 7,
        "license_token": "lic",
        "can_stream_hq": True,
        "can_stream_lossless": True,
        "country": "NL",
    }
    assert client.client.http_headers["Accept-Language"] == "nl"
    cookies = client.session.cookie_jar.update_cookies.call_args.args[0]
    assert cookies == {"arl": "the-arl"}
    _, url, kwargs = client.session.requests[0]
    assert url.startswith("https://")  # deezer-py sent the ARL over http
    assert kwargs["params"]["api_token"] == "null"


async def test_an_arl_deezer_does_not_take_is_no_login():
    client = _dz({"error": [], "results": {"USER": {"USER_ID": 0}}})
    assert not await client.client.login_via_arl("expired")


async def test_gw_renews_a_stale_token_and_follows_a_fallback():
    client = _dz(
        {"error": [], "results": {"checkForm": "csrf-1"}},
        {"error": {"VALID_TOKEN_REQUIRED": "Invalid CSRF token"}},
        {"error": [], "results": {"checkForm": "csrf-2"}},
        {"error": {"DATA_ERROR": "x"}, "payload": {"FALLBACK": {"SNG_ID": "9"}}},
        {"error": [], "results": {"SNG_ID": "9"}},
    )
    assert await client.client.gw.get_track("1") == {"SNG_ID": "9"}
    calls = [
        (k["params"]["method"], k["params"]["api_token"], k["json"])
        for _, _, k in client.session.requests
    ]
    assert calls == [
        ("deezer.getUserData", "null", {}),
        ("song.getData", "csrf-1", {"SNG_ID": "1"}),
        ("deezer.getUserData", "null", {}),
        ("song.getData", "csrf-2", {"SNG_ID": "1"}),
        ("song.getData", "csrf-2", {"SNG_ID": "9"}),
    ]


async def test_gw_error_without_a_way_out_is_raised():
    client = _dz(
        {"error": [], "results": {"checkForm": "t"}},
        {"error": {"DATA_ERROR": "song not found"}},
    )
    with pytest.raises(DeezerError, match="song not found"):
        await client.client.gw.get_track("1")


async def test_public_api_waits_out_its_quota_error(monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("streamrip.client.deezer.asyncio.sleep", sleep)
    client = _dz({"error": {"code": 4, "message": "Quota limit exceeded"}}, {"id": 5})
    assert await client.client.api.get_album(5) == {"id": 5}
    assert waits == [5]
    waits.clear()
    quota = {"error": {"code": 4, "message": "Quota limit exceeded"}}
    with pytest.raises(DeezerError, match="Quota"):
        await _dz(*[quota] * 13).client.api.get_album(7)
    assert waits == [5] * 12
    with pytest.raises(DeezerError, match="no data"):
        await _dz({"error": {"code": 800, "message": "no data"}}).client.api.get_album(
            6
        )


@pytest.mark.parametrize(
    ("user", "fmt", "reply", "expected"),
    [
        ({}, "FLAC", None, None),
        (
            {"license_token": "l", "can_stream_lossless": False},
            "FLAC",
            None,
            WrongLicenseError,
        ),
        (
            {"license_token": "l", "can_stream_hq": False},
            "MP3_320",
            None,
            WrongLicenseError,
        ),
        (
            {"license_token": "l"},
            "MP3_128",
            {"data": [{"errors": [{"code": 2002}]}]},
            WrongGeolocationError,
        ),
        (
            {"license_token": "l"},
            "MP3_128",
            {"data": [{"media": [{"sources": [{"url": "u"}]}]}]},
            "u",
        ),
        ({"license_token": "l"}, "MP3_128", {"data": [{"media": []}]}, None),
        ({"license_token": "l"}, "MP3_128", ({}, 429), None),
    ],
)
async def test_track_url(user, fmt, reply, expected):
    client = _dz(reply)
    client.client.current_user = user
    if isinstance(expected, type):
        with pytest.raises(expected):
            await client.client.get_track_url("tok", fmt)
    else:
        assert await client.client.get_track_url("tok", fmt) == expected
