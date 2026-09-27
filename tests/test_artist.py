import asyncio

import pytest

from streamrip.config import QobuzDiscographyFilterConfig
from streamrip.media.artist import RESOLVE_CHUNK_SIZE, Artist


class _FakeAlbum:
    def __init__(self):
        self.ripped = False

    async def rip(self):
        self.ripped = True


class _FakePendingAlbum:
    """Stands in for PendingAlbum: tracks how many resolve() calls are in
    flight at once, without needing a real Client/Config/API response.
    """

    _concurrent = 0
    _max_concurrent = 0

    async def resolve(self):
        type(self)._concurrent += 1
        type(self)._max_concurrent = max(
            type(self)._max_concurrent, type(self)._concurrent
        )
        await asyncio.sleep(0)  # yield, so overlapping calls can interleave
        type(self)._concurrent -= 1
        return _FakeAlbum()


NO_FILTERS = QobuzDiscographyFilterConfig(
    extras=False,
    repeats=False,
    non_albums=False,
    features=False,
    non_studio_albums=False,
    non_remaster=False,
)


@pytest.mark.asyncio
async def test_resolve_then_download_chunks_the_resolve_phase():
    """Used when qobuz_filters.repeats is on: resolving every album's title
    upfront is required, but that shouldn't mean firing them all at once.
    """
    _FakePendingAlbum._concurrent = 0
    _FakePendingAlbum._max_concurrent = 0
    albums = [_FakePendingAlbum() for _ in range(RESOLVE_CHUNK_SIZE * 3 + 1)]
    artist = Artist(name="Test Artist", albums=albums, client=None, config=None)

    await artist._resolve_then_download(NO_FILTERS)

    assert _FakePendingAlbum._max_concurrent <= RESOLVE_CHUNK_SIZE
