import asyncio
import contextlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest
import tomlkit
from aiohttp import web
from playwright.async_api import Error as PlaywrightError

from streamrip.client.qobuz import FORMAT_IDS, QobuzClient, file_url_signature
from streamrip.config import Config
from streamrip.exceptions import APIError, AuthenticationError, MissingCredentialsError
from streamrip.metadata import PlaylistMetadata
from streamrip.rip import browser_login
from streamrip.rip import qobuz_token_capture as capture
from streamrip.rip.main import Main
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
    return QobuzClient(config)


@pytest.fixture
def session(monkeypatch):
    session = MagicMock(headers={})
    session.close = AsyncMock()
    monkeypatch.setattr("streamrip.client.client.new_session", lambda **_: session)
    return session


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr("streamrip.client.qobuz.asyncio.sleep", AsyncMock())


# --- logging in ---------------------------------------------------------------


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
    """A free account can't stream, but can still download the albums it bought."""
    c = _with_creds()
    login = {"user": {"credential": {"parameters": parameters}}, "user_auth_token": "u"}
    c._api_request = AsyncMock(return_value=(200, login))
    c._get_valid_secret = AsyncMock(return_value="s1")
    await c.login()
    assert (c.logged_in, c.download_only) == (True, download_only)


@pytest.mark.login
async def test_a_refused_login_does_not_quote_the_token(session):
    c = _with_creds()
    c._api_request = AsyncMock(return_value=(401, {}))
    with pytest.raises(AuthenticationError) as err:
        await c.login()
    assert TOKEN not in str(err.value)
    assert "user_auth_token" in str(err.value)
    session.close.assert_awaited_once()  # no aiohttp session left open


@pytest.mark.parametrize(
    ("download_only", "intent"), [(True, "download"), (False, "stream")]
)
async def test_the_file_url_request_is_signed_for_its_intent(download_only, intent):
    """Qobuz answers 400 when the params and the signed preimage disagree."""
    c = _client((200, {}))
    c.download_only = download_only
    await c._request_file_url("19512574", 3, "abc123secret")
    params = c._api_request.call_args.args[1]
    assert params["intent"] == intent
    assert params["request_sig"] == file_url_signature(
        FORMAT_IDS[2], intent, "19512574", params["request_ts"], "abc123secret"
    )


def test_file_url_signature_known_answer():
    assert (
        file_url_signature(27, "stream", "19512574", 1700000000.5, "secret")
        == "7e51324fbe3fb7a052f218264ed520f1"
    )


# --- failed requests ----------------------------------------------------------
# `streamrip search qobuz ...` crashed with a bare `AssertionError: 400`: Qobuz's
# search backend failing for a moment, the same search fine seconds later.


@pytest.mark.parametrize(
    ("status", "body"),
    [(400, ALGOLIA), (501, {}), (505, {})],
    ids=["search backend", "501", "505"],  # 5xx outside the shared retry set
)
async def test_a_failure_that_may_pass_is_tried_once_more(no_wait, status, body):
    c = _client((status, body), (200, {"ok": True}))
    assert await c._request_ok("artist/search", {}) == {"ok": True}
    assert c._api_request.await_count == 2


@pytest.mark.parametrize(
    ("replies", "message"),
    [
        ([(400, ALGOLIA)] * 2, "Algolia"),
        ([(400, {"message": "Invalid parameter"})], "Invalid parameter"),
        # The shared retries had these already: not again.
        *[([(status, {})], str(status)) for status in (500, 502, 503, 504)],
    ],
)
async def test_a_failed_search_raises_qobuzs_message(no_wait, replies, message):
    c = _client(*replies)
    with pytest.raises(APIError, match=message):
        await c.search("artist", "Radioaktivists", limit=100)
    assert c._api_request.await_count == len(replies)


class _HtmlErrorPage:
    status = 502
    content_type = "text/html"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        # What aiohttp does with an HTML body when it does not check the type.
        raise json.JSONDecodeError("Expecting value", "<html>", 0)


async def test_an_html_error_page_is_reported_by_status_without_the_url(no_wait):
    """Qobuz takes credentials as URL parameters."""
    c = QobuzClient.__new__(QobuzClient)
    c.rate_limiter = contextlib.nullcontext()
    c.session = MagicMock()
    c.session.get = MagicMock(return_value=_HtmlErrorPage())
    status, page = await c._api_request(
        "user/login", {"user_id": "123456789", "user_auth_token": TOKEN}
    )
    assert status == 502
    assert "non-JSON" in page["message"]
    assert TOKEN not in str(page)


async def test_a_failed_search_command_says_so_instead_of_crashing(monkeypatch):
    m = Main.__new__(Main)
    client = MagicMock()
    client.search = AsyncMock(
        side_effect=APIError("Qobuz artist/search failed (HTTP 400): Algolia")
    )
    m.get_logged_in_client = AsyncMock(return_value=client)
    console = MagicMock()
    monkeypatch.setattr("streamrip.rip.main.console", console)
    await m.search_interactive("qobuz", "artist", "Radioaktivists")
    printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
    assert "Search failed" in printed
    assert "Algolia" in printed


# --- search pages -------------------------------------------------------------
# Response metadata must not control how many requests are made.


def _page(total=10, limit=2, offset=0):
    return {"albums": {"total": total, "limit": limit, "offset": offset, "items": []}}


def _searcher(page) -> QobuzClient:
    """A client whose every search request gets `page`."""
    client = QobuzClient.__new__(QobuzClient)

    def request(*_):
        # Fails even if requests are allocated in a synchronous loop; done
        # futures leak no unawaited coroutines then.
        assert client._request_ok.call_count <= 10, "Request budget exceeded"
        result = asyncio.get_running_loop().create_future()
        result.set_result(page)
        return result

    client._request_ok = Mock(side_effect=request)
    return client


@pytest.mark.parametrize(
    ("field", "value"),
    [("limit", v) for v in (0, -1, -(10**100), None, "2", 1.5, True, 11)]
    + [("total", v) for v in (-1, None, "10", 1.5, True)],
)
async def test_bad_pagination_metadata_fails_after_one_request(field, value):
    client = _searcher(_page(**{field: value}))
    with pytest.raises(APIError, match=f"pagination {field}"):
        await client.search("album", "query", limit=10)
    assert client._request_ok.call_count == 1


@pytest.mark.parametrize("offset", [-(10**100), -1, 0, 10**100, None, "bad"])
async def test_response_offsets_cannot_change_request_progress(offset):
    client = _searcher(_page(total=10**100, limit=2, offset=offset))
    pages = await client.search("album", "query", limit=5)
    params = [call.args[1] for call in client._request_ok.call_args_list]
    assert params == [
        {"query": "query", "limit": 5, "offset": 0},
        {"query": "query", "limit": 2, "offset": 2},
        {"query": "query", "limit": 1, "offset": 4},
    ]
    assert len(pages) == 3


@pytest.mark.parametrize("limit", [-1, None, "10", 1.5, True, False, tomlkit.item(-1)])
async def test_a_bad_search_limit_is_refused_without_a_request(limit):
    client = _searcher(_page())
    with pytest.raises(ValueError, match="search limit"):
        await client.search("album", "query", limit=limit)
    client._request_ok.assert_not_called()


NO_PAGE_SIZE = {"albums": {"total": 10, "items": []}}


@pytest.mark.parametrize(
    ("limit", "page", "pages", "requests"),
    [
        (0, _page(), 0, 0),
        (tomlkit.item(0), NO_PAGE_SIZE, 0, 0),  # config values are tomlkit Integers
        (tomlkit.item(1), NO_PAGE_SIZE, 1, 1),
        (tomlkit.item(5), NO_PAGE_SIZE, 1, 1),  # no page size: one of up to `limit`
        (5, NO_PAGE_SIZE, 1, 1),
        (10, {"albums": {"total": 0, "items": []}}, 0, 1),  # nothing found
        (10, _page(total=1), 1, 1),
        (10, _page(total=2), 1, 1),
        (10, _page(total=4), 2, 2),
        (10, _page(total=5), 3, 3),
    ],
)
async def test_a_search_asks_for_the_pages_it_needs(limit, page, pages, requests):
    client = _searcher(page)
    assert await client.search("album", "query", limit=limit) == [page] * pages
    assert client._request_ok.call_count == requests


async def test_search_accepts_the_default_config_limit():
    limit = Config.defaults().session.cli.max_search_results
    page = {"artists": {"total": 1, "limit": 1, "items": [{"name": "Eminem"}]}}
    client = _searcher(page)
    assert await client.search("artist", "Eminem", limit=limit) == [page]
    assert client._request_ok.call_args.args[1]["limit"] == limit


async def test_pages_are_requested_one_by_one_and_returned_in_order():
    client = QobuzClient.__new__(QobuzClient)
    pages = [_page(total=6, offset=0), _page(limit=0, offset=-1), _page(offset=4)]

    async def request(*args):
        assert client._request_ok.call_count == client._request_ok.await_count
        return pages[client._request_ok.await_count - 1]

    client._request_ok = AsyncMock(side_effect=request)
    assert await client.search("album", "query", limit=6) == pages
    offsets = [call.args[1]["offset"] for call in client._request_ok.call_args_list]
    assert offsets == [0, 2, 4]


@pytest.mark.parametrize("media_type", ["artist", "label"])
async def test_artists_and_labels_past_500_albums_get_all_of_them(media_type):
    async def reply(epoint, params):
        assert epoint == f"{media_type}/get"
        start = params["offset"]
        items = [{"id": i} for i in range(start, min(start + 500, 1234))]
        return {"albums_count": 1234, "albums": {"items": items}}

    client = QobuzClient(Config.defaults())
    client._request_ok = AsyncMock(side_effect=reply)
    resp = await client.get_metadata("1", media_type)
    assert [a["id"] for a in resp["albums"]["items"]] == list(range(1234))


# --- playlists ----------------------------------------------------------------
# playlist/get inlines one page (500 tracks) at most and, since Qobuz's July
# 2026 API change, often none: the full list of ids is in `track_ids`.


@pytest.mark.parametrize(
    ("resp", "ids"),
    [
        (
            {"tracks": {"items": [TRACK], "total": 3}, "track_ids": [1, 2, 3]},
            ["1", "2", "3"],
        ),
        ({"tracks": {"items": []}, "track_ids": [7, 8]}, ["7", "8"]),
        ({"tracks": {"items": [TRACK], "total": 1}}, [str(TRACK["id"])]),
        ({"tracks": {"items": [{"id": 1}, TRACK]}}, [str(TRACK["id"])]),
        ({"tracks": {"items": [None, "x", TRACK]}}, [str(TRACK["id"])]),
    ],
    ids=["cut short", "empty", "complete", "a malformed track", "not tracks"],
)
def test_a_playlists_track_ids(resp, ids):
    assert PlaylistMetadata.from_resp({"name": "P", **resp}, "qobuz").ids == ids


@pytest.mark.parametrize(("track_ids", "offsets"), [(None, [500, 1000]), ([1, 2], [])])
async def test_a_long_playlist_is_paged_only_without_track_ids(track_ids, offsets):
    c = QobuzClient.__new__(QobuzClient)
    c._request_ok = AsyncMock(
        side_effect=[
            {"tracks": {"items": [{"id": i} for i in range(500, 1000)]}},
            {"tracks": {"items": [{"id": i} for i in range(1000, 1200)]}},
        ]
    )
    resp = {
        "tracks": {"items": [{"id": i} for i in range(500)], "total": 1200},
        "track_ids": track_ids,
    }
    await c._fetch_remaining_playlist_tracks(
        "playlist/get", {"playlist_id": "x", "limit": 500, "offset": 0}, resp
    )
    asked = [call.args[1]["offset"] for call in c._request_ok.call_args_list]
    assert asked == offsets
    items = [t["id"] for t in resp["tracks"]["items"]]
    assert items == list(range(1200 if offsets else 500))


# --- the login prompt ---------------------------------------------------------


@pytest.fixture
def captures(monkeypatch):
    """A desktop, where all three ways are offered; both captures fail."""
    monkeypatch.setattr("streamrip.rip.prompter.browser_login_available", lambda: True)
    monkeypatch.setattr("streamrip.rip.prompter.has_screen", lambda: True)
    script = AsyncMock(side_effect=capture.QobuzTokenCaptureError("timed out"))
    window = AsyncMock(side_effect=capture.QobuzTokenCaptureError("no browser"))
    monkeypatch.setattr("streamrip.rip.prompter.capture_qobuz_auth_token", script)
    monkeypatch.setattr(
        "streamrip.rip.prompter.capture_qobuz_auth_token_via_browser", window
    )
    return SimpleNamespace(script=script, window=window)


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
@pytest.mark.parametrize("opened", [0, 1, OSError("No browser available")])
async def test_the_token_by_hand_when_the_script_gets_nothing(
    monkeypatch, prompter, launch, opened
):
    key = "side_effect" if isinstance(opened, Exception) else "return_value"
    launch.configure_mock(**{key: opened})
    printed = MagicMock()
    monkeypatch.setattr("streamrip.rip.prompter.console.print", printed)
    prompt = _ask(monkeypatch, "2", " 123 ", " test-token ")

    await prompter.prompt_and_login()
    shown = " ".join(str(c.args[0]) for c in printed.call_args_list).lower()
    prompter.save()

    launch.assert_called_with(LOGIN_URL)
    assert prompt.call_args.kwargs["password"] is True
    prompter.client.login.assert_awaited_once()
    cfg = prompter.config.file.qobuz
    assert (cfg.user_id, cfg.auth_token) == ("123", "test-token")
    assert LOGIN_URL in shown  # over SSH or in a container, the only way there
    assert "email" not in shown
    assert "password" not in shown


@pytest.mark.login
@pytest.mark.parametrize(("choice", "way"), [("1", "window"), ("2", "script")])
async def test_a_captured_token_needs_no_typing(
    monkeypatch, prompter, captures, launch, choice, way
):
    found = getattr(captures, way)
    found.side_effect, found.return_value = None, ("456", "captured-token")
    prompt = _ask(monkeypatch, choice)

    await prompter.prompt_and_login()
    prompter.save()

    prompt.assert_called_once()
    prompter.client.login.assert_awaited_once()
    cfg = prompter.config.file.qobuz
    assert (cfg.user_id, cfg.auth_token) == ("456", "captured-token")


@pytest.mark.login
async def test_saved_credentials_skip_every_prompt(monkeypatch, prompter, launch):
    cfg = prompter.config.session.qobuz
    cfg.user_id, cfg.auth_token = "123", "saved-token"
    prompt = _ask(monkeypatch)
    await prompter.prompt_and_login()
    launch.assert_not_called()
    prompt.assert_not_called()
    prompter.client.login.assert_awaited_once()


@pytest.mark.login
async def test_by_hand_asks_again_until_both_are_filled(monkeypatch, prompter, launch):
    prompt = _ask(monkeypatch, "3", "", " 123 ", "", " token ")
    await prompter.prompt_and_login()
    prompter.save()
    cfg = prompter.config.file.qobuz
    assert (cfg.user_id, cfg.auth_token) == ("123", "token")
    assert prompt.call_count == 5


@pytest.mark.login
async def test_without_a_screen_it_goes_straight_to_entering_it_by_hand(
    monkeypatch, prompter, captures, launch
):
    """In a container or over SSH the browser is elsewhere: it can't reach the
    window or the script's 127.0.0.1, so there is nothing to choose."""
    monkeypatch.setattr("streamrip.rip.prompter.browser_login_available", lambda: False)
    monkeypatch.setattr("streamrip.rip.prompter.has_screen", lambda: False)
    prompt = _ask(monkeypatch, "123", "token")

    await prompter.prompt_and_login()
    prompter.save()

    assert prompt.call_count == 2
    assert prompt.call_args.kwargs["password"] is True
    captures.script.assert_not_awaited()
    captures.window.assert_not_awaited()
    cfg = prompter.config.file.qobuz
    assert (cfg.user_id, cfg.auth_token) == ("123", "token")


# --- the token from a pasted script -------------------------------------------

CALLBACK_RE = re.compile(r"http://127\.0\.0\.1:\d+/callback/[\w-]+")


def _mock_console_print(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(
        "streamrip.rip.qobuz_token_capture.console.print",
        lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
    )
    return printed


async def _wait_for_callback_url(printed: list[str]) -> str:
    for _ in range(100):
        match = CALLBACK_RE.search("\n".join(printed))
        if match:
            return match.group(0)
        await asyncio.sleep(0.02)
    raise AssertionError("capture never printed a callback URL")


@pytest.mark.login
async def test_the_script_posts_the_token(monkeypatch):
    printed = _mock_console_print(monkeypatch)
    task = asyncio.ensure_future(capture.capture_qobuz_auth_token(timeout_s=5))
    callback_url = await _wait_for_callback_url(printed)

    async with (
        aiohttp.ClientSession() as session,
        session.post(
            callback_url,
            json={"user_id": "123", "token": "captured-token"},
            headers={"Origin": "https://play.qobuz.com"},
        ) as resp,
    ):
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "https://play.qobuz.com"

    assert await task == ("123", "captured-token")


@pytest.mark.login
async def test_the_script_capture_times_out_when_nothing_is_posted(monkeypatch):
    _mock_console_print(monkeypatch)
    with pytest.raises(capture.QobuzTokenCaptureError):
        await capture.capture_qobuz_auth_token(timeout_s=0.2)


@pytest.mark.login
@pytest.mark.parametrize(
    ("nonce", "payload", "status"),
    [("wrong-nonce", {"user_id": "x", "token": "y"}, 404), (None, {"no": 1}, 400)],
    ids=["wrong address", "malformed"],
)
async def test_a_bad_post_is_refused_and_the_wait_goes_on(
    monkeypatch, nonce, payload, status
):
    printed = _mock_console_print(monkeypatch)
    task = asyncio.ensure_future(capture.capture_qobuz_auth_token(timeout_s=0.5))
    url = await _wait_for_callback_url(printed)
    if nonce:
        url = f"{url.rsplit('/', 1)[0]}/{nonce}"

    async with (
        aiohttp.ClientSession() as session,
        session.post(url, json=payload) as resp,
    ):
        assert resp.status == status

    with pytest.raises(capture.QobuzTokenCaptureError):
        await task


# --- the token from a browser window ------------------------------------------


def _html(html: str):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=html, content_type="text/html")

    return handler


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
        ({"/": _saves_login(456, "delayed-token", 1500)}, ("456", "delayed-token")),
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
    ids=["already there", "after a delay", "after a page navigation"],
)
async def test_the_window_reads_the_token_from_local_storage(serve, pages, expected):
    url = await serve({path: _html(html) for path, html in pages.items()})
    result = await capture.capture_qobuz_auth_token_via_browser(
        timeout_s=15, login_url=url, headless=True
    )
    assert result == expected


@pytest.mark.real_browser
@pytest.mark.login
@pytest.mark.parametrize(
    "html",
    [
        "<p>never logs in</p>",
        '<script>localStorage.setItem("localuser", JSON.stringify({id: 789}));</script>',
    ],
    ids=["no login", "no token"],
)
async def test_the_window_times_out_without_a_token(serve, html):
    url = await serve({"/": _html(html)})
    with pytest.raises(capture.QobuzTokenCaptureError):
        await capture.capture_qobuz_auth_token_via_browser(
            timeout_s=2, login_url=url, headless=True
        )


@pytest.fixture
def browser_flow(monkeypatch):
    """A fake browser, context and page that log in at once."""
    browser = AsyncMock()
    context = browser.new_context.return_value
    page = context.new_page.return_value
    page.evaluate.return_value = '{"id": 123, "token": "test-token"}'
    page.is_closed = MagicMock(return_value=False)  # sync in Playwright
    manager = AsyncMock()
    manager.__aenter__.return_value.chromium.launch.return_value = browser
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: manager)
    launch = AsyncMock(return_value=(browser, "test-browser"))
    monkeypatch.setattr(browser_login, "launch_installed_chromium", launch)
    return manager, launch, browser, page


@pytest.mark.login
@pytest.mark.parametrize(
    "stage", ["startup", "launch", "context", "page", "navigation"]
)
async def test_a_browser_that_fails_to_start_allows_entering_it_by_hand(
    browser_flow, stage
):
    manager, launch, browser, page = browser_flow
    operations = {
        "startup": manager.__aenter__,
        "launch": launch,
        "context": browser.new_context,
        "page": browser.new_context.return_value.new_page,
        "navigation": page.goto,
    }
    error = PlaywrightError(f"{stage} failed")
    operations[stage].side_effect = error
    with pytest.raises(capture.QobuzTokenCaptureError) as raised:
        await capture.capture_qobuz_auth_token_via_browser()
    assert raised.value.__cause__ is error
    if stage in {"context", "page", "navigation"}:
        browser.close.assert_awaited_once()


@pytest.mark.login
async def test_a_downloaded_browser_that_fails_to_launch_allows_entering_it_by_hand(
    monkeypatch, browser_flow
):
    manager, launch, _, _ = browser_flow
    launch.return_value = (None, None)
    monkeypatch.setattr(browser_login.Confirm, "ask", MagicMock(return_value=True))
    monkeypatch.setattr(browser_login, "download_playwright_chromium", AsyncMock())
    manager.__aenter__.return_value.chromium.launch.side_effect = PlaywrightError(
        "Browser launch failed"
    )
    with pytest.raises(capture.QobuzTokenCaptureError, match="Browser launch failed"):
        await capture.capture_qobuz_auth_token_via_browser()


@pytest.mark.login
async def test_a_capture_error_comes_out_as_it_is(browser_flow):
    _, launch, _, _ = browser_flow
    error = capture.QobuzTokenCaptureError("Already handled")
    launch.side_effect = error
    with pytest.raises(capture.QobuzTokenCaptureError) as raised:
        await capture.capture_qobuz_auth_token_via_browser()
    assert raised.value is error


@pytest.mark.login
async def test_a_poll_during_a_page_reload_is_tried_again(monkeypatch, browser_flow):
    _, _, browser, page = browser_flow
    page.evaluate.side_effect = [
        PlaywrightError("Execution context was destroyed"),
        '{"id": 123, "token": "test-token"}',
    ]
    sleep = AsyncMock()
    monkeypatch.setattr(capture.asyncio, "sleep", sleep)
    assert await capture.capture_qobuz_auth_token_via_browser() == ("123", "test-token")
    assert page.evaluate.await_count == 2
    sleep.assert_awaited_once_with(1)
    browser.close.assert_awaited_once()


@pytest.mark.login
async def test_a_closed_login_window_stops_the_wait(browser_flow):
    _, _, _, page = browser_flow
    page.evaluate.side_effect = PlaywrightError("Target page has been closed")
    page.is_closed.return_value = True
    with pytest.raises(capture.QobuzTokenCaptureError, match="closed"):
        await capture.capture_qobuz_auth_token_via_browser()
