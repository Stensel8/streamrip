"""Search menu behavior with a real Textual app and synthetic, local covers."""

import asyncio
import builtins
import io
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from PIL import Image
from textual.widgets import Static

from streamrip.config import Config
from streamrip.metadata import SearchResults, Summary
from streamrip.metadata.search_results import album_summary, track_summary
from streamrip.rip import cover_preview
from streamrip.rip import main as main_module
from streamrip.rip.cover_preview import BlockCover, CoverPane
from streamrip.rip.main import Main
from streamrip.rip.search_menu import CoverCache, SearchMenu


def summaries(*urls):
    names = ("First Album", "Second Album", "Third Album")
    return SearchResults(
        [
            Summary(
                "album",
                str(i),
                names[i],
                artist="The Artist",
                image_urls=(url,) if url else (),
                details=[("Released", "2026-10-03"), ("Tracks", "12")],
            )
            for i, url in enumerate(urls or (None, None, None))
        ]
    )


@asynccontextmanager
async def menu(results=None, *, source="qobuz", size=(100, 40), **kwargs):
    app = SearchMenu(results or summaries(), source, "album", "test", **kwargs)
    try:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            yield app, pilot
    finally:
        await app.covers.close()


def png(color="red", size=(32, 32)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def covers(monkeypatch):
    """Every cover is a red PNG."""
    fetch = AsyncMock(return_value=png())
    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    return fetch


def details(app) -> str:
    return str(app.query_one("#details", Static).content)


async def shown(app, pilot):
    """Wait until the covers asked for are in."""
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.parametrize(
    ("keys", "chosen"),
    [
        (("down", "enter"), (1,)),  # no marks: the highlighted one
        # Marks survive a filter, and give the indices from before it.
        (("space", "slash", "s", "e", "c", "enter"), (0, 1)),
        (("space", "down", "enter"), (0, 1)),
        (("space", "escape"), None),
    ],
    ids=["highlighted", "filtered", "marked", "escape"],
)
async def test_keys_choose_results(keys, chosen):
    async with menu() as (app, pilot):
        await pilot.press(*keys)
    assert app.return_value == chosen


async def test_a_filter_without_matches_cannot_be_accepted():
    async with menu() as (app, pilot):
        await pilot.press("slash", "z", "z", "z")
        await pilot.pause()
        assert app.current is None
        assert "No matching results" in details(app)
        await pilot.press("enter", "enter")
        assert app.is_running


async def test_choosing_queues_the_original_results_only(monkeypatch):
    main = Main.__new__(Main)
    main.config = Config.defaults()
    main._search = AsyncMock(return_value=summaries())
    main.add_all_by_id = AsyncMock()
    choose = AsyncMock(return_value=(2, 0))
    # main's own name: patching search_menu's would miss it and open the real menu.
    monkeypatch.setattr(main_module, "choose_results", choose)

    await main.search_interactive("qobuz", "album", "test")
    main.add_all_by_id.assert_awaited_once_with(
        [("qobuz", "album", "2"), ("qobuz", "album", "0")]
    )

    choose.return_value = None  # the menu was closed
    main.add_all_by_id.reset_mock()
    await main.search_interactive("qobuz", "album", "test")
    main.add_all_by_id.assert_not_awaited()


async def test_deezer_album_details_arrive_once_highlighted(monkeypatch):
    album = {
        "id": 7,
        "title": "Visions",
        "artist": {"name": "The Artist"},
        "record_type": "album",
        "nb_tracks": 12,
        "release_date": "2024-03-08",
        "duration": 2734,
        "genres": {"data": [{"name": "Jazz"}]},
        "label": "Blue Note Records",
    }
    monkeypatch.setattr(
        CoverCache, "_fetch", AsyncMock(return_value=json.dumps(album).encode())
    )
    found = album_summary(
        {"id": 7, "title": "Visions", "artist": {"name": "The Artist"}}
    )
    async with menu(SearchResults([found]), source="deezer") as (app, pilot):
        await shown(app, pilot)
        assert dict(app.results[0].details) == {
            "Released": "2024-03-08",
            "Type": "Album",
            "Tracks": "12",
            "Length": "45:34",
            "Genre": "Jazz",
            "Label": "Blue Note Records",
        }


async def test_a_cover_shows_when_it_arrives(monkeypatch):
    ready = asyncio.Event()

    async def fetch(self, url):
        await ready.wait()
        return png()

    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu(summaries("cover")) as (app, pilot):
        pane = app.query_one(CoverPane)
        assert pane.image is None
        ready.set()
        await shown(app, pilot)
        assert pane.image is not None
        assert pane.query_one(BlockCover)


async def test_moving_on_drops_the_old_cover_and_shows_the_new(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def fetch(self, url):
        if url == "slow":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return png("blue")

    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu(summaries("slow", "fast")) as (app, pilot):
        await asyncio.wait_for(started.wait(), 2)
        await pilot.press("down")
        await shown(app, pilot)
        assert cancelled.is_set()
        assert app.query_one(CoverPane).image.getpixel((0, 0)) == (0, 0, 255)


@pytest.mark.parametrize(
    ("urls", "fetch"),
    [
        (("broken",), AsyncMock(side_effect=OSError("offline"))),
        (("broken",), AsyncMock(return_value=b"not an image")),
        ((), AsyncMock()),  # no artwork: nothing to download
    ],
    ids=["offline", "not an image", "no artwork"],
)
async def test_without_a_cover_the_menu_stays_usable(monkeypatch, urls, fetch):
    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu(summaries(*urls)) as (app, pilot):
        await shown(app, pilot)
        assert app.query_one(CoverPane).image is None
        assert "First Album" in details(app)
        await pilot.press("enter")
    assert app.return_value == (0,)


async def test_the_cache_tries_the_smaller_variant_when_the_large_one_is_missing(serve):
    async def missing(request):
        return web.Response(status=404)

    async def cover(request):
        return web.Response(body=png(), content_type="image/png")

    url = await serve({"/missing": missing, "/cover": cover})
    cache = CoverCache()
    try:
        assert await cache.get((url + "missing", url + "cover")) is not None
    finally:
        await cache.close()


@pytest.mark.parametrize(
    ("image_fields", "expected"),
    [
        (
            {"image": {"large": "600", "small": "300", "thumbnail": "50"}},
            ("600", "300", "50"),
        ),
        (
            {"cover_big": "500", "cover_medium": "250", "cover_small": "56"},
            ("500", "250", "56"),
        ),
        (
            {"artwork_url": "https://cdn/art-large.jpg"},
            ("https://cdn/art-t500x500.jpg", "https://cdn/art-large.jpg"),
        ),
    ],
    ids=["qobuz", "deezer", "soundcloud"],
)
def test_provider_covers_prefer_the_larger_variant(image_fields, expected):
    assert album_summary({"id": 1, **image_fields}).image_urls == expected
    if "artwork_url" not in image_fields:
        assert track_summary({"id": 1, "album": image_fields}).image_urls == expected


async def test_the_cover_gives_way_on_a_small_terminal(covers):
    async with menu(summaries("cover"), size=(60, 24)) as (app, pilot):
        await shown(app, pilot)
        pane = app.query_one(CoverPane)
        assert not pane.display
        await pilot.resize_terminal(140, 50)
        await pilot.pause(0.2)
        assert pane.display
        assert pane.region.right < app.query_one("#details").region.x
        await pilot.resize_terminal(40, 15)
        await pilot.pause(0.2)
        assert not pane.display
        assert app.query_one("#details").size.width > 0


def test_a_failed_terminal_probe_uses_blocks(monkeypatch):
    monkeypatch.setattr(cover_preview.sys, "stdin", Mock(isatty=lambda: True))
    monkeypatch.setattr(cover_preview.sys, "stdout", Mock(isatty=lambda: True))
    original = builtins.__import__

    def fail(name, *args, **kwargs):
        if name == "textual_image":
            raise OSError("terminal probe failed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail)
    assert cover_preview.image_widget() is None


async def test_a_pixel_renderer_that_fails_falls_back_to_blocks(covers):
    broken_widget = Mock(side_effect=RuntimeError("encoder failed"))
    async with menu(summaries("cover"), pixel_widget=broken_widget) as (app, pilot):
        await shown(app, pilot)
        pane = app.query_one(CoverPane)
        assert pane.query_one(BlockCover)
        assert pane.pixel_widget is None
