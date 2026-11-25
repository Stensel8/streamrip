import contextlib
import copy
import json
import os
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest
from aiohttp import web
from Cryptodome.Cipher import Blowfish

from streamrip.client.deezer import (
    FORMATS,
    DeezerClient,
    DeezerError,
    WrongGeolocationError,
    WrongLicenseError,
)
from streamrip.client.downloadable import DEEZER_STRIPE, DeezerDownloadable
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError
from streamrip.rip import browser_login
from streamrip.rip.deezer_arl_capture import (
    DeezerArlCaptureError,
    capture_deezer_arl_via_browser,
)
from streamrip.rip.prompter import DeezerPrompter

FREE = {"license_token": "x"}
PREMIUM = FREE | {"can_stream_hq": True}
HIFI = PREMIUM | {"can_stream_lossless": True}


def _client(lower_quality=True, user=None) -> DeezerClient:
    """A client at quality 2 over a mocked deezer-py; `user` is the account."""
    config = Config.defaults()
    config.session.deezer.lower_quality_if_not_available = lower_quality
    client = DeezerClient(config)
    client.client = AsyncMock()
    client.client.current_user = user or {}  # {}: unknown, Deezer decides
    client.session = Mock()
    return client


def _track(flac, mp3_320, mp3_128, **extra) -> dict:
    """A track's gw data, with these FILESIZE_*."""
    return {
        "FILESIZE_FLAC": flac,
        "FILESIZE_MP3_320": mp3_320,
        "FILESIZE_MP3_128": mp3_128,
        "TRACK_TOKEN": "token",
        **extra,
    }


LISTED = _track(25_000_000, 5_000_000, 2_000_000)
ALL = ("FLAC", "MP3_320", "MP3_128")


def _serves(*formats, unlicensed=()):
    """A get_track_url for a track Deezer has a URL for in `formats`."""

    def get_track_url(token, fmt):
        if fmt in unlicensed:
            raise WrongLicenseError(fmt)
        if fmt in formats:
            return f"https://cdn/x.{'flac' if fmt == 'FLAC' else 'mp3'}"
        return None

    return get_track_url


@pytest.mark.parametrize(
    ("track", "served", "user", "quality", "got"),
    [
        (LISTED, _serves(*ALL), {}, 2, "FLAC"),
        (_track(0, 5, 2), _serves("MP3_320", "MP3_128"), {}, 2, "MP3_320"),
        # Deezer often lists 0 for a format it does serve.
        (_track(0, 5, 2), _serves(*ALL), {}, 2, "FLAC"),
        (LISTED, _serves(*ALL, unlicensed=["FLAC"]), {}, 2, "MP3_320"),
        # Never more than the account may stream (upstream #1015).
        (LISTED, _serves(*ALL), FREE, 1, "MP3_128"),
        (LISTED, _serves(*ALL), HIFI, 4, "FLAC"),  # `--quality 4` was an IndexError
    ],
)
async def test_the_tier_a_track_comes_in(track, served, user, quality, got):
    client = _client(user=user)
    client.client.gw.get_track.return_value = track
    client.client.get_track_url.side_effect = served
    dl = await client.get_downloadable("123", quality=quality)
    assert dl.quality == FORMATS.index(got)
    assert dl.extension == ("flac" if got == "FLAC" else "mp3")


@pytest.mark.parametrize(
    ("settings", "served", "error"),
    [
        # A size but no URL is a failed request, not a missing format: stepping
        # down would swap FLAC for MP3.
        ({}, _serves("MP3_320", "MP3_128"), "no download URL"),
        ({"lower_quality": False}, _serves("MP3_320"), "fallback is disabled"),
        ({"lower_quality": False, "user": FREE}, _serves(), "subscription"),
    ],
)
async def test_a_track_without_a_url_says_why(settings, served, error):
    client = _client(**settings)
    client.client.gw.get_track.return_value = LISTED
    client.client.get_track_url.side_effect = served
    with pytest.raises(NonStreamableError, match=error):
        await client.get_downloadable("123", quality=2)


async def test_a_delisted_track_is_downloaded_as_its_fallback():
    # Old-catalog tracks get no URL: FALLBACK names the release that replaced
    # them, and the file is decrypted with that track's id.
    client = _client()
    delisted = _track(0, 0, 0, TRACK_TOKEN="dead", FALLBACK={"SNG_ID": "456"})
    client.client.gw.get_track.side_effect = lambda track_id: (
        delisted if track_id == "123" else LISTED
    )
    client.client.get_track_url.side_effect = lambda token, fmt: (
        None if token == "dead" else "https://test.flac"
    )
    dl = await client.get_downloadable("123", quality=2)
    assert (dl.id, dl.quality) == ("456", 2)


async def test_an_old_album_id_is_followed_to_the_new_one():
    # Upstream #893: /album/<old id>/tracks has no data, the new id does.
    client = _client()
    client.client.api.get_album.return_value = {"id": 723513301, "title": "New"}

    def tracks(album_id):
        if str(album_id) == "997825":
            raise Exception("DataException: no data")
        return {"data": [{"id": 1, "disk_number": 1}]}

    client.client.api.get_album_tracks.side_effect = tracks
    resp = await client._get_album("997825")
    assert resp["track_total"] == 1


@pytest.mark.parametrize(
    ("sizes", "quality", "user", "tier"),
    [
        ([(1, 1, 1), (1, 1, 1)], 2, {}, 2),
        ([(1, 1, 1), (1, 1, 0)], 2, {}, 1),  # one without FLAC: an MP3 320 album
        ([(1, 1, 1)], 1, {}, 1),
        ([(1, 1, 1)], 2, FREE, 0),  # a FLAC folder name for MP3 downloads is a bug
    ],
)
async def test_an_albums_tier_is_its_weakest_track_within_the_account(
    sizes, quality, user, tier
):
    client = _client(user=user)
    client.config.quality = quality
    client.client.gw.get_album_tracks.return_value = [
        {"FILESIZE_MP3_128": a, "FILESIZE_MP3_320": b, "FILESIZE_FLAC": c}
        for a, b, c in sizes
    ]
    assert await client._album_quality("1") == tier


async def test_a_playlist_falls_back_to_the_gw_api():
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


async def test_loved_albums_are_a_label_of_the_profile():
    client = _client()
    client.client.gw.get_user_albums.return_value = [{"id": 11}, {"id": "12"}]
    resp = await client.get_metadata("favorites:99", "label")
    assert resp["albums"] == [{"id": "11"}, {"id": "12"}]


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
    client.rate_limiter = contextlib.nullcontext()
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


@pytest.mark.login
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
    _, url, _ = client.session.requests[0]
    assert url.startswith("https://")  # deezer-py sent the ARL over http


@pytest.mark.login
async def test_an_expired_arl_is_no_login():
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
    assert not client.session.replies


async def test_gw_error_is_raised():
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
    quota = {"error": {"code": 4, "message": "Quota limit exceeded"}}
    assert await _dz(quota, {"id": 5}).client.api.get_album(5) == {"id": 5}
    assert waits == [5]


URL = {"data": [{"media": [{"sources": [{"url": "u"}]}]}]}
LIMITED = ({}, 429)


@pytest.mark.parametrize(
    ("user", "fmt", "replies", "expected"),
    [
        (FREE | {"can_stream_lossless": False}, "FLAC", [], WrongLicenseError),
        (
            FREE,
            "MP3_128",
            [{"data": [{"errors": [{"code": 2002}]}]}],
            WrongGeolocationError,
        ),
        (FREE, "MP3_128", [URL], "u"),
        # Waited out: taken for "no such format", it made a track look delisted.
        (FREE, "MP3_128", [LIMITED, URL], "u"),
        (FREE, "MP3_128", [LIMITED] * 4, None),
    ],
    ids=["no licence", "wrong country", "a url", "rate limited", "rate limited on"],
)
async def test_track_url(monkeypatch, user, fmt, replies, expected):
    monkeypatch.setattr("streamrip.client.deezer.asyncio.sleep", AsyncMock())
    client = _dz(*replies)
    client.client.current_user = user
    if isinstance(expected, type):
        with pytest.raises(expected):
            await client.client.get_track_url("tok", fmt)
    else:
        assert await client.client.get_track_url("tok", fmt) == expected


IV = b"\x00\x01\x02\x03\x04\x05\x06\x07"


def _encrypt(key: bytes, plain: bytes) -> bytes:
    """What Deezer serves: the first 2048 bytes of every stripe encrypted."""
    out = bytearray(plain)
    for i in range(0, len(out), DEEZER_STRIPE):
        if len(out) - i >= 2048:
            block = bytes(out[i : i + 2048])
            out[i : i + 2048] = Blowfish.new(key, Blowfish.MODE_CBC, IV).encrypt(block)
    return bytes(out)


class _Cdn:
    """A CDN that serves `body`, in chunks of irregular size."""

    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}
        self.content = self

    def get(self, _url, **_):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return json.loads(self.body)

    async def iter_chunked(self, _n):
        sizes, pos, i = [1000, 7000, 333, 20000], 0, 0
        while pos < len(self.body):
            size = sizes[i % len(sizes)]
            yield self.body[pos : pos + size]
            pos, i = pos + size, i + 1


def _downloadable(body: bytes, sizes=(0, 0, 5_000_000)) -> DeezerDownloadable:
    return DeezerDownloadable(
        _Cdn(body),
        url="https://cdn.example/mobile/1/track",
        quality=2,
        track_id="3135556",
        sizes=list(sizes),
    )


async def test_an_encrypted_stream_decrypts_to_the_original(tmp_path):
    plain = os.urandom(5 * DEEZER_STRIPE + 3000)
    key = DeezerDownloadable._generate_blowfish_key("3135556")
    received = []

    downloadable = _downloadable(_encrypt(key, plain), (0, 0, len(plain)))
    await downloadable._download(str(tmp_path / "t.flac"), received.append)

    assert (tmp_path / "t.flac").read_bytes() == plain
    assert sum(received) == len(plain)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'{"error": "RIGHTS", "message": "Not available"}', "RIGHTS - Not available"),
        (b"<html>gone</html>", "File not found."),
    ],
)
async def test_an_error_body_instead_of_audio_is_non_streamable(
    tmp_path, body, message
):
    with pytest.raises(NonStreamableError, match=message):
        await _downloadable(body)._download(str(tmp_path / "t.flac"), lambda _: None)


LOGIN_URL = "https://www.deezer.com/login"


@pytest.fixture
def window(monkeypatch):
    """A desktop, where the browser window is offered; it gets no ARL."""
    monkeypatch.setattr("streamrip.rip.prompter.browser_login_available", lambda: True)
    capture = AsyncMock(side_effect=DeezerArlCaptureError("no browser found"))
    monkeypatch.setattr(
        "streamrip.rip.prompter.capture_deezer_arl_via_browser", capture
    )
    return capture


@pytest.fixture
def prompter(window):
    config = Config.defaults()
    client = DeezerClient(config)
    client.login = AsyncMock()
    return DeezerPrompter(config, client)


@pytest.fixture
def launch(monkeypatch):
    launch = MagicMock(return_value=0)
    monkeypatch.setattr("streamrip.rip.prompter.launch", launch)
    return launch


def _ask(monkeypatch, *answers):
    """Answer the prompts with `answers`, in order."""
    prompt = MagicMock(side_effect=list(answers))
    monkeypatch.setattr("streamrip.rip.prompter.Prompt.ask", prompt)
    return prompt


@pytest.mark.login
@pytest.mark.parametrize(
    ("choice", "opened"),
    [("1", 0), ("2", OSError("No browser available"))],
    ids=["the window got none", "no browser to open"],
)
async def test_the_arl_by_hand(monkeypatch, prompter, launch, choice, opened):
    key = "side_effect" if isinstance(opened, Exception) else "return_value"
    launch.configure_mock(**{key: opened})
    printed = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", printed)
    prompt = _ask(monkeypatch, choice, "", " typed-arl ")  # a blank one is asked again

    await prompter.prompt_and_login()
    prompter.save()

    shown = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
    assert LOGIN_URL in shown  # over SSH or in a container, the only way there
    assert prompt.call_args.kwargs["password"] is True
    assert prompter.config.file.deezer.arl == "typed-arl"


@pytest.mark.login
async def test_an_arl_from_the_browser_window(monkeypatch, prompter, window, launch):
    window.side_effect, window.return_value = None, "captured-arl"
    _ask(monkeypatch, "1")

    await prompter.prompt_and_login()
    prompter.save()

    assert prompter.config.file.deezer.arl == "captured-arl"


@pytest.mark.login
async def test_a_saved_arl_skips_every_prompt(monkeypatch, prompter, launch):
    prompter.config.session.deezer.arl = "saved-arl"
    prompt = _ask(monkeypatch)

    await prompter.prompt_and_login()

    prompt.assert_not_called()
    prompter.client.login.assert_awaited_once()


@pytest.mark.login
async def test_without_a_screen_it_goes_straight_to_entering_it_by_hand(
    monkeypatch, prompter, window, launch
):
    monkeypatch.setattr("streamrip.rip.prompter.browser_login_available", lambda: False)
    prompt = _ask(monkeypatch, "typed-arl")

    await prompter.prompt_and_login()
    prompter.save()

    prompt.assert_called_once()
    window.assert_not_awaited()
    assert prompter.config.file.deezer.arl == "typed-arl"


@pytest.mark.real_browser
@pytest.mark.login
async def test_the_window_reads_the_httponly_arl_cookie(serve):
    async def login_page(request: web.Request) -> web.Response:
        response = web.Response(text="<p>login</p>", content_type="text/html")
        response.set_cookie("arl", "the-arl", httponly=True)
        return response

    url = await serve({"/": login_page})
    found = await capture_deezer_arl_via_browser(
        timeout_s=15, login_url=url, headless=True
    )
    assert found == "the-arl"


@pytest.mark.login
async def test_declining_the_browser_download_allows_entering_it_by_hand(monkeypatch):
    monkeypatch.setattr(
        browser_login, "launch_installed_chromium", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(browser_login.Confirm, "ask", MagicMock(return_value=False))
    with pytest.raises(DeezerArlCaptureError, match="Skipped downloading"):
        await capture_deezer_arl_via_browser()
