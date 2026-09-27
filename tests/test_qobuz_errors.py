"""Qobuz failures should explain themselves, and never quote credentials.

`rip search qobuz ...` crashed with a bare `AssertionError: 400`. The 400 was
Qobuz's search backend failing transiently -- "Impossible to connect, please
check your Algolia Application Id." -- and the same search succeeded seconds
later. Separately, Qobuz takes credentials as URL query parameters, and both an
HTML error page and a failed login used to put them into error output.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.client.qobuz import QobuzClient
from streamrip.exceptions import APIError, AuthenticationError

ALGOLIA = {
    "message": "Impossible to connect, please check your Algolia Application Id."
}
TOKEN = "SECRET-TOKEN-abcdefghij"


@pytest.fixture(autouse=True)
def no_wait():
    with patch("streamrip.client.qobuz.asyncio.sleep", new=AsyncMock()):
        yield


def _client(*responses):
    c = QobuzClient.__new__(QobuzClient)
    c._api_request = AsyncMock(side_effect=list(responses))
    return c


@pytest.mark.asyncio
async def test_transient_failure_is_retried_once():
    ok = {"artists": {"items": [], "total": 0}}
    c = _client((400, ALGOLIA), (200, ok))
    assert await c._request_ok("artist/search", {}) == ok
    assert c._api_request.await_count == 2


@pytest.mark.asyncio
async def test_persistent_failure_raises_with_qobuz_message():
    c = _client((400, ALGOLIA), (400, ALGOLIA))
    with pytest.raises(APIError, match="Algolia"):
        await c.search("artist", "Radioaktivists", limit=100)
    assert c._api_request.await_count == 2


@pytest.mark.asyncio
async def test_other_errors_are_not_retried():
    c = _client((400, {"message": "Invalid parameter"}))
    with pytest.raises(APIError, match="Invalid parameter"):
        await c._request_ok("artist/search", {})
    assert c._api_request.await_count == 1


class _HtmlErrorPage:
    status = 502
    content_type = "text/html"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        raise AssertionError("must not parse an HTML page as JSON")


@pytest.mark.asyncio
async def test_html_error_page_is_reported_by_status_without_the_url():
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


@pytest.mark.asyncio
async def test_failed_login_does_not_quote_the_token():
    c = QobuzClient.__new__(QobuzClient)
    c.logged_in = False
    c.config = MagicMock()
    q = c.config.session.qobuz
    q.use_auth_token = True
    q.email_or_userid = "123456789"
    q.password_or_token = TOKEN
    q.app_id = "987654321"
    q.secrets = ["s1"]
    c._api_request = AsyncMock(return_value=(401, {}))
    session = MagicMock()
    session.close = AsyncMock()
    with patch.object(QobuzClient, "get_session", new=AsyncMock(return_value=session)):
        with pytest.raises(AuthenticationError) as err:
            await c.login()
    assert TOKEN not in str(err.value)
    assert "user_auth_token" in str(err.value)
    # A failed login must not leave the aiohttp session open.
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_command_reports_failure_instead_of_crashing():
    from streamrip.rip.main import Main

    m = Main.__new__(Main)
    client = MagicMock()
    client.search = AsyncMock(
        side_effect=APIError("Qobuz artist/search failed (HTTP 400): Algolia")
    )
    m.get_logged_in_client = AsyncMock(return_value=client)
    with patch("streamrip.rip.main.console") as console:
        await m.search_interactive("qobuz", "artist", "Radioaktivists")
    printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
    assert "Search failed" in printed and "Algolia" in printed
