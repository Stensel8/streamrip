import asyncio
import contextlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import tomlkit
from aiohttp import web
from playwright.async_api import Error as PlaywrightError

from streamrip.client.qobuz import QobuzClient, file_url_signature
from streamrip.config import Config
from streamrip.exceptions import (
    AccountHalt,
    APIError,
    AuthenticationError,
    MissingCredentialsError,
    NonStreamableError,
)
from streamrip.metadata import PlaylistMetadata
from streamrip.rip import browser_login
from streamrip.rip import qobuz_token_capture as capture
from streamrip.rip.prompter import QobuzPrompter

LOGIN_URL = "https://play.qobuz.com/login"
TOKEN = "SECRET-TOKEN-abcdefghij"
ALGOLIA = {
    "message": "Impossible to connect, please check your Algolia Application Id."
}
TRACK = json.loads(Path("tests/qobuz_track_resp.json").read_text(encoding="utf-8"))


def _client(*replies) -> QobuzClient:
    """A client whose API answers with `replies`, a (status, body) each, in turn."""
    c = QobuzClient.__new__(QobuzClient)
    c._api_request = AsyncMock(side_effect=list(replies))
    return c


def _with_creds() -> QobuzClient:
    config = Config.defaults()
    q = config.session.qobuz
    q.user_id, q.auth_token = "123456789", TOKEN
    # Cached, so login() does not fetch them from Qobuz's web player.
    q.app_id, q.secrets = "987654321", ["s1"]
    client = QobuzClient(config)
    client.rate_limiter = contextlib.nullcontext()
    return client


@pytest.fixture
def session(monkeypatch):
    session = MagicMock(headers={})
    session.close = AsyncMock()
    monkeypatch.setattr("streamrip.client.client.new_session", lambda **_: session)
    return session


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr("streamrip.client.qobuz.asyncio.sleep", AsyncMock())


async def test_the_login_goes_with_api_requests_only(serve, monkeypatch, tmp_path):
    # The same session fetches the files, from whatever address an answer names.
    seen: dict[str, set[str]] = {}
    login = {"user": {"credential": {"parameters": {"hires": True}}}}

    async def answer(request):
        assert TOKEN not in request.query_string  # in a header, as the web player
        # streamrip says what it is, and nothing of a browser's.
        assert request.headers["User-Agent"].startswith("streamrip/")
        seen[f"{request.method} {request.path}"] = {
            name.lower() for name in request.headers
        }
        if request.path == "/file":
            return web.Response(body=b"audio" * 100)
        return web.json_response(
            {**login, "user_auth_token": TOKEN, "url": f"{base}file"}
        )

    base = await serve(
        dict.fromkeys(("/user/login", "/track/getFileUrl", "/file"), answer)
    )
    monkeypatch.setattr("streamrip.client.qobuz.QOBUZ_BASE_URL", base.rstrip("/"))
    client = _with_creds()
    try:
        await client.login()
        downloadable = await client.get_downloadable("1", 2)
        await downloadable.size()
        await downloadable.download(tmp_path / "track.flac", lambda _: None)
    finally:
        await client.close()

    credentials = {"x-app-id", "x-user-auth-token"}
    assert credentials <= seen["POST /user/login"] & seen["GET /track/getFileUrl"]
    assert not credentials & (seen["HEAD /file"] | seen["GET /file"])
    assert not any("sec-ch-ua" in names for names in seen.values())
    assert (tmp_path / "track.flac").read_bytes() == b"audio" * 100


@pytest.mark.login
async def test_no_credentials_is_missing_credentials():
    with pytest.raises(MissingCredentialsError):
        await QobuzClient(Config.defaults()).login()


@pytest.mark.login
@pytest.mark.parametrize(
    ("parameters", "download_only"),
    [({"lossy_streaming": True, "hires_streaming": True}, False), ([], True)],
    ids=["subscriber", "free account"],
)
async def test_an_account_that_cannot_stream_is_download_only(
    session, parameters, download_only
):
    # A free account can't stream, but can still download the albums it bought.
    c = _with_creds()
    login = {"user": {"credential": {"parameters": parameters}}, "user_auth_token": "u"}
    c._api_request = AsyncMock(return_value=(200, login))
    await c.login()
    assert (c.logged_in, c.download_only) == (True, download_only)


@pytest.mark.login
async def test_a_refused_login_does_not_quote_the_token(session):
    c = _with_creds()
    c._api_request = AsyncMock(return_value=(401, {}))
    with pytest.raises(AuthenticationError) as err:
        await c.login()
    assert TOKEN not in str(err.value)
    session.close.assert_awaited_once()  # no aiohttp session left open


def _logged_in(*replies, download_only=False) -> QobuzClient:
    """A logged-in client whose file URL requests are answered with `replies`."""
    c = _with_creds()
    c.secret, c.logged_in, c.download_only = "s1", True, download_only
    c.session = MagicMock()
    c._request_file_url = AsyncMock(side_effect=list(replies))
    return c


async def test_a_region_locked_track_is_not_streamable():
    restricted = {"restrictions": [{"code": "TrackRestrictedByTerritory"}]}
    with pytest.raises(NonStreamableError):
        await _logged_in((200, restricted)).get_downloadable("1", 3)


async def test_a_blocked_account_stops_the_run_with_qobuzs_word():
    # Seen 2026-11-27: the signature was right, and the account got a 403.
    c = _logged_in()
    c.download_only = False
    del c._request_file_url  # the real one, over an API that answers so
    c._api_request = AsyncMock(return_value=(403, {"message": "Account is blocked"}))
    with pytest.raises(AccountHalt, match="blocked"):
        await c.get_downloadable("1", 3)


async def test_a_wrong_secret_gives_way_to_the_one_that_works_and_stays_first():
    # Of the web player's three the last one worked (2026-11-27). None is tested
    # at login: a wrong one shows at the first file URL, and only then.
    wrong = (400, {"message": "Invalid Request Signature parameter (request_sig)"})
    c = _logged_in(wrong, wrong, (200, {"url": "https://cdn/x.flac", "format_id": 6}))
    c.config.secrets = ["s1", "s2", "s3"]
    await c.get_downloadable("1", 2)
    signed = [call.args[2] for call in c._request_file_url.await_args_list]
    assert signed == ["s1", "s2", "s3"]
    assert c.config.secrets[0] == c.secret == "s3"


async def test_a_login_gone_mid_run_stops_the_run():
    # Seen 2026-11-26: once Qobuz dropped the login, every request got a 401.
    c = _logged_in()
    c._get_json = AsyncMock(return_value=(401, {"message": "User authentication"}))
    with pytest.raises(AccountHalt, match="log in"):
        await c.get_metadata("1", "album")


async def test_the_login_page_code_is_traded_as_the_web_player_does():
    c = _with_creds()

    async def bundle():  # what the web player's bundle says
        c._private_key = "KEY"
        return "798273057", ["s3"]

    c._get_app_id_and_secrets = bundle
    link = await c.oauth_link("http://127.0.0.1:5000/qobuz/abc")
    assert link == (
        "https://www.qobuz.com/signin/oauth?ext_app_id=798273057"
        "&redirect_url=http%3A%2F%2F127.0.0.1%3A5000%2Fqobuz%2Fabc"
    )
    c._api_request = AsyncMock(return_value=(200, {"token": "T", "user_id": 42}))
    try:
        assert await c.oauth_token("CODE") == ("42", "T")
    finally:
        await c.close()
    c._api_request.assert_awaited_once_with(
        "oauth/callback", {"code": "CODE", "private_key": "KEY"}
    )


async def test_a_purchased_track_steps_down_to_the_quality_it_was_sold_in():
    only_cd = {"restrictions": [{"code": "FormatRestrictedByFormatAvailability"}]}
    c = _logged_in(
        (200, only_cd),
        (200, {"url": "https://cdn/x.flac", "format_id": 6}),
        download_only=True,
    )
    dl = await c.get_downloadable("1", 3)
    assert (dl.extension, dl.quality) == ("flac", 2)


def test_file_url_signature_known_answer():
    assert (
        file_url_signature(27, "stream", "19512574", 1700000000.5, "secret")
        == "7e51324fbe3fb7a052f218264ed520f1"
    )


# `streamrip search qobuz ...` crashed with a bare `AssertionError: 400`: Qobuz's
# search backend failing for a moment, the same search fine seconds later.
@pytest.mark.parametrize(
    ("status", "body"), [(400, ALGOLIA), (501, {})], ids=["search backend", "501"]
)
async def test_a_failure_that_may_pass_is_tried_once_more(no_wait, status, body):
    c = _client((status, body), (200, {"ok": True}))
    assert await c._request_ok("artist/search", {}) == {"ok": True}


@pytest.mark.parametrize(
    ("replies", "message"),
    [
        ([(400, ALGOLIA)] * 2, "Algolia"),
        ([(400, {"message": "Invalid parameter"})], "Invalid parameter"),
        ([(500, {})], "500"),
    ],
)
async def test_a_failed_search_raises_qobuzs_message(no_wait, replies, message):
    c = _client(*replies)
    with pytest.raises(APIError, match=message):
        await c.search("artist", "Radioaktivists", limit=100)


async def test_an_html_error_page_is_reported_by_status_without_the_url(no_wait):
    # Qobuz takes credentials as URL parameters.
    resp = MagicMock(status=502, content_type="text/html")
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = False
    resp.json = AsyncMock(side_effect=json.JSONDecodeError("Expecting value", "", 0))
    c = QobuzClient.__new__(QobuzClient)
    c.rate_limiter = contextlib.nullcontext()
    c._login_headers = {}
    c.session = MagicMock(get=MagicMock(return_value=resp))

    status, page = await c._api_request("user/login", {"user_auth_token": TOKEN})

    assert status == 502
    assert TOKEN not in str(page)


def _page(total=10, limit=2):
    return {"albums": {"total": total, "limit": limit, "items": []}}


@pytest.mark.parametrize(
    ("limit", "page", "pages"),
    [
        (10, _page(total=5), 3),
        (10, _page(total=0), 0),  # nothing found
        # Config values are tomlkit Integers; this page has no page size.
        (tomlkit.item(5), {"albums": {"total": 10, "items": []}}, 1),
    ],
)
async def test_a_search_asks_for_the_pages_it_needs(limit, page, pages):
    client = QobuzClient.__new__(QobuzClient)
    client._request_ok = AsyncMock(return_value=page)
    assert await client.search("album", "query", limit=limit) == [page] * pages


@pytest.mark.parametrize("media_type", ["artist", "label"])
async def test_artists_and_labels_past_500_albums_get_all_of_them(media_type):
    async def reply(epoint, params):
        start = params["offset"]
        items = [{"id": i} for i in range(start, min(start + 500, 1234))]
        return {"albums_count": 1234, "albums": {"items": items}}

    client = QobuzClient(Config.defaults())
    client._request_ok = AsyncMock(side_effect=reply)
    resp = await client.get_metadata("1", media_type)
    assert [a["id"] for a in resp["albums"]["items"]] == list(range(1234))


# playlist/get inlines at most 500 tracks and, since Qobuz's July 2026 API
# change, often none: the full list of ids is in `track_ids`.
@pytest.mark.parametrize(
    ("resp", "ids"),
    [
        (
            {"tracks": {"items": [TRACK], "total": 3}, "track_ids": [1, 2, 3]},
            ["1", "2", "3"],
        ),
        ({"tracks": {"items": []}, "track_ids": [7, 8]}, ["7", "8"]),
        ({"tracks": {"items": [TRACK], "total": 1}}, [str(TRACK["id"])]),
    ],
    ids=["cut short", "empty", "complete"],
)
def test_a_playlists_track_ids(resp, ids):
    assert PlaylistMetadata.from_resp({"name": "P", **resp}, "qobuz").ids == ids


async def test_a_long_playlist_is_paged_without_track_ids():
    c = QobuzClient.__new__(QobuzClient)
    c._request_ok = AsyncMock(
        side_effect=[
            {"tracks": {"items": [{"id": i} for i in range(500, 1000)]}},
            {"tracks": {"items": [{"id": i} for i in range(1000, 1200)]}},
        ]
    )
    resp = {"tracks": {"items": [{"id": i} for i in range(500)], "total": 1200}}
    await c._fetch_remaining_playlist_tracks(
        "playlist/get", {"playlist_id": "x", "limit": 500, "offset": 0}, resp
    )
    assert [t["id"] for t in resp["tracks"]["items"]] == list(range(1200))


@pytest.fixture
def captures(monkeypatch):
    """A desktop, where every way is offered; each of them fails."""
    monkeypatch.setattr("streamrip.rip.prompter.browser_login_available", lambda: True)
    monkeypatch.setattr("streamrip.rip.prompter.has_screen", lambda: True)
    script = AsyncMock(side_effect=capture.QobuzTokenCaptureError("timed out"))
    window = AsyncMock(side_effect=capture.QobuzTokenCaptureError("no browser"))
    monkeypatch.setattr("streamrip.rip.prompter.capture_qobuz_auth_token", script)
    monkeypatch.setattr(
        "streamrip.rip.prompter.capture_qobuz_auth_token_via_browser", window
    )
    oauth = AsyncMock(side_effect=AuthenticationError("Qobuz did not take it"))
    monkeypatch.setattr("streamrip.rip.prompter.QobuzPrompter._oauth", oauth)
    return SimpleNamespace(script=script, window=window, oauth=oauth)


@pytest.fixture
def prompter(captures):
    config = Config.defaults()
    client = QobuzClient(config)
    client.login = AsyncMock()
    return QobuzPrompter(config, client)


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
@pytest.mark.parametrize("opened", [0, OSError("No browser available")])
async def test_the_token_by_hand_when_the_script_gets_nothing(
    monkeypatch, prompter, launch, opened
):
    key = "side_effect" if isinstance(opened, Exception) else "return_value"
    launch.configure_mock(**{key: opened})
    printed = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", printed)
    prompt = _ask(monkeypatch, "3", " 123 ", " test-token ")

    await prompter.prompt_and_login()
    prompter.save()

    shown = " ".join(str(c.args[0]) for c in printed.call_args_list)
    assert LOGIN_URL in shown  # over SSH or in a container, the only way there
    assert prompt.call_args.kwargs["password"] is True
    cfg = prompter.config.file.qobuz
    assert (cfg.user_id, cfg.auth_token) == ("123", "test-token")


@pytest.mark.login
@pytest.mark.parametrize(
    ("choice", "way"), [("1", "oauth"), ("2", "window"), ("3", "script")]
)
async def test_a_captured_token_needs_no_typing(
    monkeypatch, prompter, captures, launch, choice, way
):
    found = getattr(captures, way)
    found.side_effect, found.return_value = None, ("456", "captured-token")
    prompt = _ask(monkeypatch, choice)

    await prompter.prompt_and_login()
    prompter.save()

    prompt.assert_called_once()
    cfg = prompter.config.file.qobuz
    assert (cfg.user_id, cfg.auth_token) == ("456", "captured-token")


@pytest.mark.login
async def test_saved_credentials_skip_every_prompt(monkeypatch, prompter, launch):
    cfg = prompter.config.session.qobuz
    cfg.user_id, cfg.auth_token = "123", "saved-token"
    prompt = _ask(monkeypatch)
    await prompter.prompt_and_login()
    prompt.assert_not_called()
    prompter.client.login.assert_awaited_once()


@pytest.mark.login
async def test_without_a_screen_it_is_qobuzs_own_page_or_by_hand(
    monkeypatch, prompter, captures, launch
):
    # In a container or over SSH the browser is elsewhere: no script, no window.
    monkeypatch.setattr("streamrip.rip.prompter.browser_login_available", lambda: False)
    monkeypatch.setattr("streamrip.rip.prompter.has_screen", lambda: False)
    prompt = _ask(monkeypatch, "2", "123", "token")

    await prompter.prompt_and_login()
    prompter.save()

    assert prompt.call_args_list[0].kwargs["choices"] == ["1", "2"]
    assert prompt.call_count == 3
    captures.script.assert_not_awaited()
    captures.window.assert_not_awaited()
    cfg = prompter.config.file.qobuz
    assert (cfg.user_id, cfg.auth_token) == ("123", "token")


@pytest.mark.login
async def test_the_script_posts_the_token(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(
        "streamrip.rip.qobuz_token_capture.console.print",
        lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
    )
    task = asyncio.ensure_future(capture.capture_qobuz_auth_token(timeout_s=5))
    for _ in range(100):  # until the script prints the address to post to
        match = re.search(
            r"http://127\.0\.0\.1:\d+/callback/[\w-]+", "\n".join(printed)
        )
        if match:
            break
        await asyncio.sleep(0.02)

    async with (
        aiohttp.ClientSession() as session,
        session.post(
            match[0],
            json={"user_id": "123", "token": "captured-token"},
            headers={"Origin": "https://play.qobuz.com"},
        ) as resp,
    ):
        assert resp.status == 204

    assert await task == ("123", "captured-token")


def _saves_login(id_, token, after_ms=None) -> str:
    """A page that saves a login to local storage, as Qobuz's web player does."""
    save = f'localStorage.setItem("localuser", JSON.stringify({{id: {id_}, token: "{token}"}}))'
    if after_ms is not None:
        save = f"setTimeout(() => {save}, {after_ms})"
    return f"<script>{save};</script>"


@pytest.mark.real_browser
@pytest.mark.login
@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ({"/": _saves_login(123, "abc-token")}, ("123", "abc-token")),
        # Qobuz reloads the page on login, which destroys the JS context mid-poll
        # (seen live 2026-09-30: it used to crash the whole capture).
        (
            {
                "/": '<script>window.location.href = "/after";</script>',
                "/after": _saves_login(999, "post-nav-token", 200),
            },
            ("999", "post-nav-token"),
        ),
    ],
    ids=["already there", "after a page navigation"],
)
async def test_the_window_reads_the_token_from_local_storage(serve, pages, expected):
    def html(text):
        async def handler(request: web.Request) -> web.Response:
            return web.Response(text=text, content_type="text/html")

        return handler

    url = await serve({path: html(text) for path, text in pages.items()})
    result = await capture.capture_qobuz_auth_token_via_browser(
        timeout_s=15, login_url=url, headless=True
    )
    assert result == expected


@pytest.fixture
def browser_flow(monkeypatch):
    """A fake browser, context and page that log in at once."""
    browser = AsyncMock()
    page = browser.new_context.return_value.new_page.return_value
    page.evaluate.return_value = '{"id": 123, "token": "test-token"}'
    page.is_closed = MagicMock(return_value=False)  # sync in Playwright
    launch = AsyncMock(return_value=(browser, "test-browser"))
    monkeypatch.setattr(browser_login, "launch_installed_chromium", launch)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: AsyncMock())
    return launch, page


@pytest.mark.login
@pytest.mark.parametrize("stage", ["launch", "navigation"])
async def test_a_browser_that_fails_allows_entering_the_token_by_hand(
    browser_flow, stage
):
    launch, page = browser_flow
    error = PlaywrightError(f"{stage} failed")
    {"launch": launch, "navigation": page.goto}[stage].side_effect = error
    with pytest.raises(capture.QobuzTokenCaptureError) as raised:
        await capture.capture_qobuz_auth_token_via_browser()
    assert raised.value.__cause__ is error


@pytest.mark.login
async def test_a_closed_login_window_stops_the_wait(browser_flow):
    _, page = browser_flow
    page.evaluate.side_effect = PlaywrightError("Target page has been closed")
    page.is_closed.return_value = True
    with pytest.raises(capture.QobuzTokenCaptureError, match="closed"):
        await capture.capture_qobuz_auth_token_via_browser()
