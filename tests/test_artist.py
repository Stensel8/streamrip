import asyncio

import pytest

from streamrip.config import ArtistFilterConfig
from streamrip.media.artist import Artist
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


NO_FILTERS = ArtistFilterConfig(
    extras=False,
    repeats=False,
    non_albums=False,
    features=False,
    non_remaster=False,
)


@pytest.mark.asyncio
async def test_albums_rip_strictly_one_at_a_time():
    """No album's tracks start until the last album's are done downloading,
    so the progress display never shows two albums' tracks at once.
    """
    concurrent = 0
    max_concurrent = 0

    class Album:
        async def rip(self):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0)
            concurrent -= 1

    class Pending:
        async def resolve(self):
            return Album()

    albums = [Pending() for _ in range(4)]
    artist = Artist(name="Test Artist", albums=albums, client=None, config=None)

    await asyncio.wait_for(artist._download_async(NO_FILTERS), 5)

    assert max_concurrent == 1


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
    """Used when artist_filters.repeats is on: resolving every album's title
    upfront is required, but that shouldn't mean firing them all at once.
    """
    _FakePendingAlbum._concurrent = 0
    _FakePendingAlbum._max_concurrent = 0
    albums = [_FakePendingAlbum() for _ in range(4)]
    artist = Artist(name="Test Artist", albums=albums, client=None, config=None)

    await artist._resolve_then_download(NO_FILTERS)

    assert _FakePendingAlbum._max_concurrent == 1


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
