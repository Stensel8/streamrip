import asyncio
import contextlib
import copy
import json
import os
from contextlib import asynccontextmanager
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
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.exceptions import AuthenticationError, NonStreamableError
from streamrip.rip import browser_login
from streamrip.rip.browser_login import BrowserLoginError
from streamrip.rip.deezer_arl_capture import (
    DeezerArlCaptureError,
    capture_deezer_arl_via_browser,
)
from streamrip.rip.prompter import DeezerPrompter, QobuzPrompter

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


# --- streams ------------------------------------------------------------------


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


# The tiers asked for: from the one wanted (within the account) down, until one
# has a URL.
@pytest.mark.parametrize(
    ("track", "served", "user", "quality", "asked"),
    [
        (_track(0, 5, 2), _serves("MP3_320", "MP3_128"), {}, 2, ["FLAC", "MP3_320"]),
        # Deezer often lists 0 for a format it does serve.
        (_track(0, 5, 2), _serves(*ALL), {}, 2, ["FLAC"]),
        (_track(0, 0, 0), _serves("FLAC"), {}, 2, ["FLAC"]),
        (LISTED, _serves(*ALL), {}, 2, ["FLAC"]),
        (_track(0, 0, 2), _serves("MP3_128"), {}, 2, ["FLAC", "MP3_320", "MP3_128"]),
        (LISTED, _serves(*ALL, unlicensed=["FLAC"]), {}, 2, ["FLAC", "MP3_320"]),
        # Never more than the account may stream (upstream #1015).
        (LISTED, _serves(*ALL), FREE, 1, ["MP3_128"]),
        (LISTED, _serves(*ALL), PREMIUM, 2, ["MP3_320"]),
        (LISTED, _serves(*ALL), HIFI, 4, ["FLAC"]),  # `--quality 4` was an IndexError
    ],
)
async def test_the_tier_a_track_comes_in(track, served, user, quality, asked):
    client = _client(user=user)
    client.client.gw.get_track.return_value = track
    client.client.get_track_url.side_effect = served
    dl = await client.get_downloadable("123", quality=quality)
    assert [c.args[1] for c in client.client.get_track_url.call_args_list] == asked
    assert dl.quality == FORMATS.index(asked[-1])
    assert dl.extension == ("flac" if asked[-1] == "FLAC" else "mp3")
    assert await dl.size() == track[f"FILESIZE_{asked[-1]}"]


@pytest.mark.parametrize(
    ("settings", "track", "served", "asked", "error"),
    [
        # A size but no URL is a failed request (deezer-py turns a 429 into
        # None), not a missing format: stepping down would swap FLAC for MP3.
        ({}, LISTED, _serves("MP3_320", "MP3_128"), ["FLAC"], "no download URL"),
        # It used to make up a URL on a CDN Deezer has retired.
        ({}, LISTED, _serves(), ["FLAC"], "no download URL"),
        # A fallback is followed once: Deezer's chains can lead to another dead one.
        (
            {},
            _track(25, 5, 2, FALLBACK={"SNG_ID": "456"}),
            _serves(),
            ["FLAC", "FLAC"],
            "delisted",
        ),
        (
            {"lower_quality": False},
            _track(0, 5, 2),
            _serves("MP3_320", "MP3_128"),
            ["FLAC"],
            "quality 2 is not available and fallback is disabled",
        ),
        ({"lower_quality": False, "user": FREE}, LISTED, _serves(), [], "subscription"),
    ],
)
async def test_a_track_without_a_url_says_why(settings, track, served, asked, error):
    client = _client(**settings)
    client.client.gw.get_track.return_value = track
    client.client.get_track_url.side_effect = served
    with pytest.raises(NonStreamableError, match=error):
        await client.get_downloadable("123", quality=2)
    assert [c.args[1] for c in client.client.get_track_url.call_args_list] == asked


async def test_a_delisted_track_is_downloaded_as_its_fallback():
    """Old-catalog tracks get no URL at any tier: they were superseded, and
    FALLBACK names the release that replaced them."""
    client = _client()
    delisted = _track(0, 0, 0, TRACK_TOKEN="dead", FALLBACK={"SNG_ID": "456"})
    client.client.gw.get_track.side_effect = lambda track_id: (
        delisted if track_id == "123" else LISTED
    )
    client.client.get_track_url.side_effect = lambda token, fmt: (
        None if token == "dead" else "https://test.flac"
    )

    dl = await client.get_downloadable("123", quality=2)

    # The Blowfish key is derived from the id, so it follows the bytes served.
    assert dl.id == "456"
    # Asked at the quality wanted, not the one the zeroed sizes fell to.
    assert dl.quality == 2


# --- albums -------------------------------------------------------------------


async def test_an_album_is_asked_for_once():
    client = _client()
    api = client.client.api
    api.get_album.return_value = {"id": "1", "title": "A", "genres": {"data": []}}
    api.get_album_tracks.return_value = {"data": []}

    albums = await asyncio.gather(*(client._get_album("1") for _ in range(20)))
    albums.append(await client._get_album("1"))  # and later, from the cache

    assert all(a is albums[0] for a in albums)
    assert albums[0]["title"] == "A"
    assert (api.get_album.call_count, api.get_album_tracks.call_count) == (1, 1)


async def test_a_failed_album_request_is_tried_again():
    client = _client()
    api = client.client.api
    api.get_album.side_effect = [
        ConnectionError("down"),
        {"id": "1", "title": "A", "genres": {"data": []}},
    ]
    api.get_album_tracks.return_value = {"data": []}

    with pytest.raises(NonStreamableError):
        await client._get_album("1")
    assert (await client._get_album("1"))["title"] == "A"


async def test_an_old_album_id_is_followed_to_the_new_one():
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


def _album_rows(*sizes):
    """GW album-track rows with the given (MP3_128, MP3_320, FLAC) sizes."""
    return [
        {"FILESIZE_MP3_128": a, "FILESIZE_MP3_320": b, "FILESIZE_FLAC": c}
        for a, b, c in sizes
    ]


# The folder name follows the best tier every track has.
@pytest.mark.parametrize(
    ("rows", "tier"),
    [
        (_album_rows((1, 1, 1), (1, 1, 1)), 2),
        (_album_rows((1, 1, 1), (1, 1, 0)), 1),  # one without FLAC: an MP3 320 album
        (_album_rows((1, 1, 1), (1, 0, 0)), 0),
        (_album_rows((1, 1, 1), (0, 0, 0)), 2),  # a delisted one: its fallback decides
        ([], 2),  # nothing known: the target
        (Exception("boom"), 2),
    ],
)
async def test_an_albums_tier_is_its_weakest_tracks(rows, tier):
    client = _client()
    key = "side_effect" if isinstance(rows, Exception) else "return_value"
    client.client.gw.get_album_tracks = AsyncMock(**{key: rows})
    assert await client._album_quality("1") == tier


@pytest.mark.parametrize(
    ("quality", "user", "tier"), [(1, {}, 1), (2, PREMIUM, 1), (2, FREE, 0)]
)
async def test_an_albums_tier_is_capped_by_config_and_account(quality, user, tier):
    """A FLAC folder name for MP3 downloads is the bug this prevents."""
    client = _client(user=user)
    client.config.quality = quality
    client.client.gw.get_album_tracks.return_value = _album_rows((1, 1, 1))
    assert await client._album_quality("1") == tier


async def test_an_albums_tier_without_lowering_is_the_target():
    client = _client(lower_quality=False)
    assert await client._album_quality("1") == 2
    client.client.gw.get_album_tracks.assert_not_called()


async def test_an_album_carries_its_tier():
    client = _client()
    client.client.api.get_album.return_value = {
        "id": "9",
        "title": "T",
        "genres": {"data": []},
    }
    client.client.api.get_album_tracks.return_value = {"data": []}
    client.client.gw.get_album_tracks.return_value = _album_rows((1, 1, 1), (1, 1, 0))

    assert (await client._get_album("9"))["stream_quality"] == 1


# --- other listings -----------------------------------------------------------


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


async def test_a_playlist_id_may_be_a_number():
    client = _client()
    client.client.api.get_playlist.return_value = {"title": "P", "nb_tracks": 0}
    client.client.api.get_playlist_tracks.return_value = {"data": []}
    resp = await client.get_metadata(123, "playlist")
    assert resp["title"] == "P"


async def test_loved_albums_are_a_label_of_the_profile():
    client = _client()
    client.client.gw.get_user_albums.return_value = [{"id": 11}, {"id": "12"}]
    resp = await client.get_metadata("favorites:99", "label")
    assert resp["albums"] == [{"id": "11"}, {"id": "12"}]
    assert client.client.gw.get_user_albums.call_args.args == ("99",)


# --- requests to Deezer -------------------------------------------------------


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
    client.rate_limiter = contextlib.nullcontext()  # its pacing has tests of its own
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
    quota = {"error": {"code": 4, "message": "Quota limit exceeded"}}
    assert await _dz(quota, {"id": 5}).client.api.get_album(5) == {"id": 5}
    assert waits == [5]
    waits.clear()
    with pytest.raises(DeezerError, match="Quota"):
        await _dz(*[quota] * 13).client.api.get_album(7)
    assert waits == [5] * 12
    no_data = {"error": {"code": 800, "message": "no data"}}
    with pytest.raises(DeezerError, match="no data"):
        await _dz(no_data).client.api.get_album(6)


@pytest.mark.parametrize(
    ("user", "fmt", "reply", "expected"),
    [
        ({}, "FLAC", None, None),
        (FREE | {"can_stream_lossless": False}, "FLAC", None, WrongLicenseError),
        (FREE | {"can_stream_hq": False}, "MP3_320", None, WrongLicenseError),
        (
            FREE,
            "MP3_128",
            {"data": [{"errors": [{"code": 2002}]}]},
            WrongGeolocationError,
        ),
        (FREE, "MP3_128", {"data": [{"media": [{"sources": [{"url": "u"}]}]}]}, "u"),
        (FREE, "MP3_128", {"data": [{"media": []}]}, None),
        (FREE, "MP3_128", ({}, 429), None),
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


async def test_deezer_requests_go_through_its_rate_limiter():
    entered = []

    class Limiter:
        async def __aenter__(self):
            entered.append(True)

        async def __aexit__(self, *_):
            return False

    client = _dz({"id": 5})
    client.rate_limiter = Limiter()
    await client.client.request("GET", "https://api.deezer.com/track/5")
    assert entered == [True]


# --- decrypting the download --------------------------------------------------

IV = b"\x00\x01\x02\x03\x04\x05\x06\x07"


def _encrypt(key: bytes, plain: bytes) -> bytes:
    """What Deezer serves: the first 2048 bytes of every stripe encrypted."""
    out = bytearray(plain)
    for i in range(0, len(out), DEEZER_STRIPE):
        if len(out) - i >= 2048:
            block = bytes(out[i : i + 2048])
            out[i : i + 2048] = Blowfish.new(key, Blowfish.MODE_CBC, IV).encrypt(block)
    return bytes(out)


class _Content:
    def __init__(self, body: bytes):
        self.body = body

    async def iter_chunked(self, _n):
        # Irregular sizes, so stripes straddle the chunks the server sends.
        sizes, pos, i = [1000, 7000, 333, 20000], 0, 0
        while pos < len(self.body):
            size = sizes[i % len(sizes)]
            yield self.body[pos : pos + size]
            pos, i = pos + size, i + 1


class _CdnResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return json.loads(self.body)


class _Cdn:
    def __init__(self, body: bytes):
        self.body = body

    def get(self, _url, **_):
        return _CdnResponse(self.body)


@pytest.mark.parametrize("length", [5 * DEEZER_STRIPE + 3000, 5 * DEEZER_STRIPE + 100])
async def test_an_encrypted_stream_decrypts_to_the_original(tmp_path, length):
    plain = os.urandom(length)
    key = DeezerDownloadable._generate_blowfish_key("3135556")
    downloadable = DeezerDownloadable(
        _Cdn(_encrypt(key, plain)),
        url="https://cdn.example/mobile/1/track",
        quality=2,
        track_id="3135556",
        sizes=[0, 0, length],
    )
    received = []

    await downloadable._download(str(tmp_path / "t.flac"), received.append)

    assert (tmp_path / "t.flac").read_bytes() == plain
    assert sum(received) == length


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
    downloadable = DeezerDownloadable(
        _Cdn(body),
        url="https://cdn.example/mobile/1/track",
        quality=2,
        track_id="1",
        sizes=[0, 0, 5_000_000],
    )

    with pytest.raises(NonStreamableError, match=message):
        await downloadable._download(str(tmp_path / "t.flac"), lambda _: None)


# --- the login prompt ---------------------------------------------------------

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
    [("1", 0), ("2", 0), ("2", OSError("No browser available"))],
    ids=["the window got none", "by hand", "no browser to open"],
)
async def test_the_arl_by_hand(monkeypatch, prompter, launch, choice, opened):
    key = "side_effect" if isinstance(opened, Exception) else "return_value"
    launch.configure_mock(**{key: opened})
    printed = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", printed)
    prompt = _ask(monkeypatch, choice, "", "  ", " typed-arl ")

    await prompter.prompt_and_login()
    shown = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args).lower()
    prompter.save()

    launch.assert_called_with(LOGIN_URL)
    # Never the window on a bare Enter: it may first download a browser (150 MB).
    assert prompt.call_args_list[0].kwargs["default"] == "2"
    assert prompt.call_count == 4  # a blank ARL is asked for again
    assert prompt.call_args.kwargs["password"] is True
    assert prompter.config.file.deezer.arl == "typed-arl"
    assert LOGIN_URL in shown  # over SSH or in a container, the only way there
    assert "email" not in shown
    assert "password" not in shown


@pytest.mark.login
@pytest.mark.parametrize("rejected", [0, 1])
async def test_an_arl_from_the_browser_window(
    monkeypatch, prompter, window, launch, rejected
):
    """One Deezer rejects brings the menu back."""
    window.side_effect, window.return_value = None, "captured-arl"
    login = prompter.client.login
    login.side_effect = [AuthenticationError("expired")] * rejected + [None]
    prompt = _ask(monkeypatch, *["1"] * (1 + rejected))

    await prompter.prompt_and_login()
    prompter.save()

    assert prompt.call_count == login.await_count == 1 + rejected
    launch.assert_not_called()
    assert prompter.config.file.deezer.arl == "captured-arl"


@pytest.mark.login
async def test_a_saved_arl_skips_every_prompt(monkeypatch, prompter, launch):
    prompter.config.session.deezer.arl = "saved-arl"
    prompt = _ask(monkeypatch)

    await prompter.prompt_and_login()

    prompt.assert_not_called()
    launch.assert_not_called()
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
    assert prompt.call_args.kwargs["password"] is True
    window.assert_not_awaited()
    launch.assert_called_with(LOGIN_URL)
    assert prompter.config.file.deezer.arl == "typed-arl"


@pytest.mark.login
def test_deezer_and_qobuz_announce_a_save_the_same_way(monkeypatch, prompter):
    qobuz = QobuzPrompter(prompter.config, QobuzClient(prompter.config))
    output = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", output)

    prompter.save()
    deezer_says = output.call_args.args[0]
    qobuz.save()
    qobuz_says = output.call_args.args[0]

    assert deezer_says == qobuz_says
    assert "saved to config file at" in deezer_says


# --- the ARL from a browser window --------------------------------------------


def _page(html="<p>login</p>", cookies=None):
    """A handler that serves `html` and sets `cookies`, HttpOnly."""

    async def handler(request: web.Request) -> web.Response:
        response = web.Response(text=html, content_type="text/html")
        for name, value in (cookies or {}).items():
            response.set_cookie(name, value, httponly=True)
        return response

    return handler


@pytest.mark.real_browser
@pytest.mark.login
@pytest.mark.parametrize(
    ("pages", "arl"),
    [
        ({"/": _page(cookies={"arl": "the-arl"})}, "the-arl"),
        (
            {
                "/": _page(
                    "<script>setTimeout(() => fetch('/logged-in'), 1500)</script>"
                ),
                "/logged-in": _page(cookies={"arl": "delayed-arl"}),
            },
            "delayed-arl",
        ),
    ],
    ids=["at once", "after a delay"],
)
async def test_the_window_reads_the_httponly_arl_cookie(serve, pages, arl):
    url = await serve(pages)
    found = await capture_deezer_arl_via_browser(
        timeout_s=15, login_url=url, headless=True
    )
    assert found == arl


@pytest.mark.real_browser
@pytest.mark.login
@pytest.mark.parametrize(
    "cookies",
    [None, {"sid": "s", "jwt": "j", "refresh-token": "r"}],
    ids=["no login", "other cookies"],
)
async def test_only_the_arl_counts_as_a_login(serve, cookies):
    """A logged-in session carries a jwt, a refresh token and payment details
    too, and none of those are wanted."""
    url = await serve({"/": _page(cookies=cookies)})
    with pytest.raises(DeezerArlCaptureError, match="No login detected"):
        await capture_deezer_arl_via_browser(timeout_s=2, login_url=url, headless=True)


def _failing_browser(error: Exception):
    """An open_login_browser that raises `error` instead of opening a browser."""

    @asynccontextmanager
    async def open_login_browser(service, headless=False):
        raise error
        yield  # pragma: no cover

    return open_login_browser


@pytest.mark.login
@pytest.mark.parametrize(
    "error",
    [
        BrowserLoginError("Skipped downloading a browser"),
        RuntimeError("the window was closed"),
    ],
)
async def test_a_browser_failure_allows_entering_it_by_hand(monkeypatch, error):
    """DeezerArlCaptureError is the one error the prompter falls back on."""
    monkeypatch.setattr(browser_login, "open_login_browser", _failing_browser(error))
    with pytest.raises(DeezerArlCaptureError, match=str(error)) as raised:
        await capture_deezer_arl_via_browser()
    assert raised.value.__cause__ is error


@pytest.mark.login
async def test_declining_the_browser_download_allows_entering_it_by_hand(
    monkeypatch,
):
    monkeypatch.setattr(
        browser_login, "launch_installed_chromium", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(browser_login.Confirm, "ask", MagicMock(return_value=False))
    with pytest.raises(DeezerArlCaptureError, match="Skipped downloading"):
        await capture_deezer_arl_via_browser()
