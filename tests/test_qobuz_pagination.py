"""Search response metadata must not control unbounded request allocation."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from streamrip.client.qobuz import QobuzClient
from streamrip.exceptions import APIError


def _page(total=10, limit=2, offset=0):
    return {"albums": {"total": total, "limit": limit, "offset": offset, "items": []}}


def _client(page, budget=10):
    client = QobuzClient.__new__(QobuzClient)

    def request(*args):
        # Fail safely even if the old synchronous allocation loop returns.
        # Completed futures avoid leaking unawaited coroutines in that case.
        assert client._request_ok.call_count <= budget, "Request budget exceeded"
        result = asyncio.get_running_loop().create_future()
        result.set_result(page)
        return result

    client._request_ok = Mock(side_effect=request)
    return client


@pytest.mark.parametrize("page_size", [0, -1, -(10**100), None, "2", 1.5, True, 11])
async def test_invalid_page_size_fails_before_allocating_more_requests(page_size):
    client = _client(_page(limit=page_size))
    with pytest.raises(APIError, match="pagination limit"):
        await client.search("album", "query", limit=10)
    assert client._request_ok.call_count == 1


@pytest.mark.parametrize("offset", [-(10**100), -1, 0, 10**100, None, "bad"])
async def test_response_offsets_cannot_change_request_progress(offset):
    client = _client(_page(total=10**100, limit=2, offset=offset))
    pages = await client.search("album", "query", limit=5)
    params = [call.args[1] for call in client._request_ok.call_args_list]
    assert params == [
        {"query": "query", "limit": 5, "offset": 0},
        {"query": "query", "limit": 2, "offset": 2},
        {"query": "query", "limit": 1, "offset": 4},
    ]
    assert len(pages) == 3


@pytest.mark.parametrize("total", [-1, None, "10", 1.5, True])
async def test_invalid_total_is_reported(total):
    client = _client(_page(total=total))
    with pytest.raises(APIError, match="pagination total"):
        await client.search("album", "query", limit=10)
    assert client._request_ok.call_count == 1


@pytest.mark.parametrize("limit", [-1, None, "10", 1.5, True])
async def test_invalid_caller_limit_is_rejected_without_a_request(limit):
    client = _client(_page())
    with pytest.raises(ValueError, match="search limit"):
        await client.search("album", "query", limit=limit)
    client._request_ok.assert_not_called()


async def test_zero_caller_limit_needs_no_request():
    client = _client(_page())
    assert await client.search("album", "query", limit=0) == []
    client._request_ok.assert_not_called()


async def test_empty_search_needs_no_pagination_metadata():
    client = _client({"albums": {"total": 0, "items": []}})
    assert await client.search("album", "query", limit=10) == []
    assert client._request_ok.call_count == 1


@pytest.mark.parametrize("total, expected_pages", [(1, 1), (2, 1), (4, 2), (5, 3)])
async def test_search_stops_at_total(total, expected_pages):
    client = _client(_page(total=total))
    pages = await client.search("album", "query", limit=10)
    assert len(pages) == expected_pages
    assert client._request_ok.call_count == expected_pages


async def test_pages_are_requested_incrementally_and_returned_in_order():
    client = QobuzClient.__new__(QobuzClient)
    pages = [_page(total=6, offset=0), _page(limit=0, offset=-1), _page(offset=4)]

    async def request(*args):
        assert client._request_ok.call_count == client._request_ok.await_count
        return pages[client._request_ok.await_count - 1]

    client._request_ok = AsyncMock(side_effect=request)
    assert await client.search("album", "query", limit=6) == pages
    assert [call.args[1]["offset"] for call in client._request_ok.call_args_list] == [
        0,
        2,
        4,
    ]


async def test_missing_page_size_uses_bounded_request_default():
    page = {"albums": {"total": 10, "items": []}}
    client = _client(page)
    assert await client.search("album", "query", limit=5) == [page]
    assert client._request_ok.call_count == 1
