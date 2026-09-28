import asyncio

import pytest

from streamrip.config import QobuzDiscographyFilterConfig
from streamrip.media.artist import RESOLVE_CHUNK_SIZE, Artist
from streamrip.media.label import Label


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
async def test_next_album_starts_as_soon_as_a_slot_frees_up():
    """One slow album must not hold back the albums queued behind it."""
    fast = RESOLVE_CHUNK_SIZE * 3
    finished = 0
    release = asyncio.Event()

    class Album:
        def __init__(self, slow):
            self.slow = slow

        async def rip(self):
            nonlocal finished
            if self.slow:
                # Only finishes once every fast album has, which can't happen
                # if the ones behind it wait for it in batches.
                await release.wait()
                return
            finished += 1
            if finished == fast:
                release.set()

    class Pending:
        def __init__(self, slow):
            self.slow = slow

        async def resolve(self):
            return Album(self.slow)

    albums = [Pending(True)] + [Pending(False) for _ in range(fast)]
    artist = Artist(name="Test Artist", albums=albums, client=None, config=None)

    await asyncio.wait_for(artist._download_async(NO_FILTERS), 5)

    assert finished == fast


@pytest.mark.asyncio
async def test_a_failing_album_does_not_stop_the_others(caplog):
    ripped = []

    class Album:
        def __init__(self, n):
            self.n = n

        async def rip(self):
            ripped.append(self.n)

    class Pending:
        def __init__(self, n, fail=False):
            self.n, self.fail = n, fail

        async def resolve(self):
            if self.fail:
                raise ConnectionError("boom")
            return Album(self.n)

    albums = [Pending(1), Pending(2, fail=True), Pending(3)]
    artist = Artist(name="Test Artist", albums=albums, client=None, config=None)

    await artist._download_async(NO_FILTERS)

    assert sorted(ripped) == [1, 3]
    assert "Error downloading album: ConnectionError: boom" in caplog.text


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


@pytest.mark.asyncio
async def test_resolve_then_download_survives_a_failing_resolve(caplog):
    ripped = []

    class Album:
        def __init__(self, n):
            self.n = n

        async def rip(self):
            ripped.append(self.n)

    class Pending:
        def __init__(self, n, fail=False):
            self.n, self.fail = n, fail

        async def resolve(self):
            if self.fail:
                raise ConnectionError("boom")
            return Album(self.n)

    albums = [Pending(1), Pending(2, fail=True), Pending(3)]
    artist = Artist(name="Test Artist", albums=albums, client=None, config=None)

    await artist._resolve_then_download(NO_FILTERS)

    assert sorted(ripped) == [1, 3]
    assert "Error resolving album: ConnectionError: boom" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_label_album_does_not_stop_the_others():
    ripped = []

    class Album:
        def __init__(self, n):
            self.n = n

        async def rip(self):
            if self.n == 2:
                raise ConnectionError("boom")
            ripped.append(self.n)

    class Pending:
        def __init__(self, n):
            self.n = n

        async def resolve(self):
            return Album(self.n)

    label = Label(
        name="Test Label",
        albums=[Pending(n) for n in (1, 2, 3)],
        client=None,
        config=None,
    )

    await label.download()

    assert sorted(ripped) == [1, 3]
