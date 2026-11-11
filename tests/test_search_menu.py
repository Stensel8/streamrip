"""Search UI behavior with a real Textual app and synthetic, local covers."""

import asyncio
import builtins
import io
import json
import random
import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from PIL import Image
from rich.control import Control
from textual.widgets import Input, OptionList, Static

from streamrip.config import Config
from streamrip.metadata import SearchResults, Summary
from streamrip.metadata.search_results import album_summary, track_summary
from streamrip.rip import cover_preview, search_menu
from streamrip.rip.cover_preview import BlockCover, CoverPane
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


def tty(monkeypatch):
    # Called in the test, not a fixture: pytest's capture sets its own stdout
    # between a fixture's setup and the test.
    monkeypatch.setattr(cover_preview.sys, "stdin", Mock(isatty=lambda: True))
    monkeypatch.setattr(cover_preview.sys, "stdout", Mock(isatty=lambda: True))


def details(app) -> str:
    return str(app.query_one("#details", Static).content)


# --- choosing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("keys", "chosen"),
    [
        (("down", "enter"), (1,)),  # no marks: the highlighted one
        # Marks survive a filter, and give the indices from before it.
        (("space", "slash", "s", "e", "c", "o", "n", "d", "enter"), (0, 1)),
        (("space", "down", "enter"), (0, 1)),  # and the highlighted one, as before
        (("space", "escape"), None),
        (("space", "ctrl+c"), None),
    ],
    ids=["highlighted", "filtered", "marked", "escape", "ctrl+c"],
)
async def test_keys_choose_results(keys, chosen):
    async with menu() as (app, pilot):
        await pilot.press(*keys)
    assert app.return_value == chosen


async def test_filter_accepts_spaces_and_escape_restores_results():
    async with menu() as (app, pilot):
        await pilot.press("slash")
        field = app.query_one(Input)
        field.value = "First"
        await pilot.press("space", "a")
        assert field.value == "First a"
        assert not app.selected
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.visible_indices) == 3
        assert app.query_one(OptionList).has_focus
        await pilot.press("escape")
    assert app.return_value is None


async def test_no_matching_results_clears_preview_and_cannot_accept():
    async with menu() as (app, pilot):
        await pilot.press("slash")
        app.query_one(Input).value = "no such album"
        await pilot.pause()
        assert app.current is None
        assert not app.query_one(CoverPane).display
        assert "No matching results" in details(app)
        await pilot.press("enter", "enter")
        assert app.is_running


async def test_choose_results_detects_before_starting_menu_and_closes_cache(
    monkeypatch,
):
    widget = object()
    app = Mock(run_async=AsyncMock(return_value=(1,)))
    app.covers.close = AsyncMock()

    def detect():
        assert not factory.called
        assert not app.run_async.called
        return widget

    factory = Mock(return_value=app)
    monkeypatch.setattr(search_menu, "image_widget", detect)
    monkeypatch.setattr(search_menu, "SearchMenu", factory)
    results = summaries()
    assert await search_menu.choose_results(
        results, "qobuz", "album", "test", verify_ssl=True
    ) == (1,)
    factory.assert_called_once_with(
        results, "qobuz", "album", "test", pixel_widget=widget, verify_ssl=True
    )
    app.covers.close.assert_awaited_once()


async def test_main_queues_original_choices_only(monkeypatch):
    from streamrip.rip import main as main_module
    from streamrip.rip.main import Main

    main = Main.__new__(Main)
    main.config = Config.defaults()
    main._search = AsyncMock(return_value=summaries())
    main.add_all_by_id = AsyncMock()
    choose = AsyncMock(return_value=(2, 0))
    # main's own (lazily imported) name: patching search_menu's would miss it
    # once main has bound it, and open the real menu.
    monkeypatch.setattr(main_module, "choose_results", choose)
    await main.search_interactive("qobuz", "album", "test")
    choose.assert_awaited_once_with(
        main._search.return_value,
        "qobuz",
        "album",
        "test",
        verify_ssl=main.config.session.downloads.verify_ssl,
    )
    main.add_all_by_id.assert_awaited_once_with(
        [("qobuz", "album", "2"), ("qobuz", "album", "0")]
    )
    choose.return_value = None
    main.add_all_by_id.reset_mock()
    await main.search_interactive("qobuz", "album", "test")
    main.add_all_by_id.assert_not_awaited()


async def test_deezer_album_details_arrive_once_highlighted(monkeypatch):
    urls = []

    async def fetch(self, url):
        urls.append(url)
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
        return json.dumps(album).encode()

    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    found = album_summary(
        {"id": 7, "title": "Visions", "artist": {"name": "The Artist"}, "nb_tracks": 12}
    )
    async with menu(SearchResults([found]), source="deezer") as (app, pilot):
        await pilot.pause(0.3)
        assert urls == ["https://api.deezer.com/album/7"]
        assert dict(app.results[0].details) == {
            "Released": "2024-03-08",
            "Type": "Album",
            "Tracks": "12",
            "Length": "45:34",
            "Genre": "Jazz",
            "Label": "Blue Note Records",
        }
        prompt = app.query_one(OptionList).get_option_at_index(0).prompt
        assert "Visions by The Artist (2024)" in str(prompt)


# --- covers -------------------------------------------------------------------


async def test_cover_arrival_refreshes_without_a_keypress(monkeypatch):
    ready = asyncio.Event()

    async def fetch(self, url):
        await ready.wait()
        return png()

    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu(summaries("cover")) as (app, pilot):
        await pilot.pause(0.15)
        assert app.query_one(CoverPane).image is None
        ready.set()
        await pilot.pause(0.2)
        pane = app.query_one(CoverPane)
        assert pane.image is not None
        assert pane.query_one(BlockCover)
        assert pane.region.right < app.query_one("#details").region.x
        assert app.query_one("#preview").region.y > app.query_one(OptionList).region.y


async def test_new_selection_cancels_old_cover_and_wins(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

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
        await pilot.pause(0.2)
        assert cancelled.is_set()
        assert app.current == 1
        assert app.query_one(CoverPane).image.getpixel((0, 0)) == (0, 0, 255)
        assert "slow" not in app.covers.images


async def test_same_cover_is_not_replaced_when_marking_or_changing_track(covers):
    async with menu(summaries("same", "same")) as (app, pilot):
        await pilot.pause(0.2)
        pane = app.query_one(CoverPane)
        child = pane.children[0]
        await pilot.press("space", "down")
        await pilot.pause()
        assert pane.children[0] is child
        assert covers.await_count == 1


async def test_background_work_is_cancelled_on_exit(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def fetch(self, url):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu(summaries("slow")) as (_, pilot):
        await asyncio.wait_for(started.wait(), 2)
        await pilot.press("escape")
    assert cancelled.is_set()


@pytest.mark.parametrize(
    ("urls", "fetch"),
    [
        (("broken",), AsyncMock(side_effect=OSError("offline"))),
        (("broken",), AsyncMock(return_value=b"not an image")),
        ((), AsyncMock()),  # no artwork: nothing to download
    ],
    ids=["offline", "not an image", "no artwork"],
)
async def test_without_a_cover_details_and_selection_stay_usable(
    monkeypatch, urls, fetch
):
    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu(summaries(*urls)) as (app, pilot):
        await pilot.pause(0.2)
        assert app.query_one(CoverPane).image is None
        assert "First Album" in details(app)
        await pilot.press("enter")
    assert app.return_value == (0,)
    assert fetch.called is bool(urls)


@pytest.mark.parametrize(("urls", "tip"), [(("cover",), True), ((), False)])
async def test_block_covers_name_terminals_that_show_images(covers, urls, tip):
    async with menu(summaries(*urls)) as (app, pilot):
        await pilot.pause(0.2)
        assert ("Ghostty" in details(app)) is tip


async def test_cache_tries_smaller_variant_and_caches_errors_and_success(serve):
    asked = []

    async def missing(request):
        asked.append("missing")
        return web.Response(status=404)

    async def cover(request):
        asked.append("cover")
        return web.Response(body=png(), content_type="image/png")

    url = await serve({"/missing": missing, "/cover": cover})
    cache = CoverCache()
    try:
        image = await cache.get((url + "missing", url + "cover"))
        assert image is not None
        assert await cache.get((url + "missing", url + "cover")) is image
        assert cache.images[url + "missing"] is None
        assert asked == ["missing", "cover"]
        session = cache.session
    finally:
        await cache.close()
    assert session.closed


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
)
def test_provider_covers_prefer_larger_variant_and_keep_fallbacks(
    image_fields, expected
):
    assert album_summary({"id": 1, **image_fields}).image_urls == expected
    if "artwork_url" not in image_fields:
        assert track_summary({"id": 1, "album": image_fields}).image_urls == expected


# --- layout -------------------------------------------------------------------


def test_resize_before_mount_is_ignored():
    # Textual sends a Resize before on_mount, when there may be no widgets yet.
    app = SearchMenu(summaries(), "qobuz", "album", "test")
    app.on_resize()
    assert app._preview_resize_timer is None


async def test_small_and_resized_layout_never_overlaps_metadata(covers):
    async with menu(summaries("cover"), size=(60, 24)) as (app, pilot):
        await pilot.pause(0.2)
        pane = app.query_one(CoverPane)
        assert not pane.display
        await pilot.resize_terminal(140, 50)
        await pilot.pause(0.2)
        assert pane.display
        assert pane.region.right < app.query_one("#details").region.x
        assert pane.region.bottom <= app.query_one("#preview").region.bottom
        await pilot.resize_terminal(40, 15)
        await pilot.pause(0.2)
        assert not pane.display
        assert app.query_one("#details").size.width > 0


# --- picking an image renderer --------------------------------------------------


@pytest.mark.parametrize("stream", ["stdin", "stdout"])
def test_auto_does_not_import_or_probe_image_library_for_redirected_io(
    monkeypatch, stream
):
    tty(monkeypatch)
    original = builtins.__import__

    def guard(name, *args, **kwargs):
        assert not name.startswith("textual_image")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(cover_preview.sys, stream, Mock(isatty=lambda: False))
    monkeypatch.setattr(builtins, "__import__", guard)
    assert cover_preview.image_widget() is None


def test_failed_terminal_probe_automatically_uses_blocks(monkeypatch):
    tty(monkeypatch)
    original = builtins.__import__

    def fail(name, *args, **kwargs):
        if name == "textual_image":
            raise OSError("terminal probe failed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail)
    assert cover_preview.image_widget() is None


@pytest.mark.parametrize("protocol", ["sixel", "tgp", "unsupported"])
def test_auto_selects_only_a_supported_pixel_renderer(monkeypatch, protocol):
    tty(monkeypatch)
    sixel, tgp, halfcell, widget = object(), object(), object(), object()
    library = ModuleType("textual_image")
    library.renderable = SimpleNamespace(
        Image={"sixel": sixel, "tgp": tgp, "unsupported": halfcell}[protocol],
        SixelImage=sixel,
        TGPImage=tgp,
    )
    library.widget = SimpleNamespace(Image=widget)
    monkeypatch.setattr(cover_preview, "_sixel_widget", lambda: widget)
    monkeypatch.setitem(sys.modules, "textual_image", library)
    assert cover_preview.image_widget() is (
        None if protocol == "unsupported" else widget
    )


# --- pixel renderers that fail ------------------------------------------------


async def test_pixel_renderer_failure_uses_blocks(covers):
    broken_widget = Mock(side_effect=RuntimeError("encoder failed"))
    async with menu(summaries("cover"), pixel_widget=broken_widget) as (app, pilot):
        await pilot.pause(0.2)
        pane = app.query_one(CoverPane)
        assert pane.query_one(BlockCover)
        assert pane.pixel_widget is None


async def test_delayed_encoder_failure_falls_back_for_later_covers(covers):
    class FailingImage(Static):
        def __init__(self, image, on_error):
            super().__init__()
            self.fallback = on_error

        async def on_mount(self):
            await self.mount(self.fallback(OSError("cannot encode")))

    async with menu(summaries("one", "two"), pixel_widget=FailingImage) as (app, pilot):
        # Until the cover is shown, not a fixed time: a busy machine is slower.
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one(CoverPane)
        assert pane.pixel_widget is None
        assert pane.query_one(BlockCover).size.height > 1
        await pilot.press("down")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(pane.children[0], BlockCover)


async def test_real_sixel_adapter_restores_last_row_and_handles_encoder_errors(
    monkeypatch, covers
):
    pixel = cover_preview._sixel_widget()
    async with menu(summaries("one"), pixel_widget=pixel) as (app, pilot):
        await pilot.pause(0.2)
        pane = app.query_one(CoverPane)
        implementation = pane.children[0].children[0]
        region = app.screen.find_widget(implementation).visible_region
        assert region.height > 1
        segments = implementation._get_sixel_segments("sixel payload")
        assert (
            segments[-1].text
            == Control.move_to(region.right, region.bottom - 1).segment.text
        )

        def fail(*args, **kwargs):
            raise RuntimeError("sixel encoding failed")

        monkeypatch.setattr(implementation, "_image_to_sixels", fail)
        implementation._cached_sixels = None
        implementation.refresh()
        await pilot.pause(0.2)
        assert pane.pixel_widget is None
        assert pane.query_one(BlockCover).size.height > 1


# --- block covers -------------------------------------------------------------


def _cell_by_brute_force(pixels):
    """_cell as it was written first: every split's means and error from scratch."""

    def mean(group):
        return tuple(sum(p[i] for p in group) // len(group) for i in range(3))

    def error(group, m):
        return sum(sum((a - b) ** 2 for a, b in zip(p, m, strict=True)) for p in group)

    order = sorted(range(6), key=lambda i: cover_preview._brightness(pixels[i]))
    best = None
    for split in range(1, 6):
        dark = [pixels[i] for i in order[:split]]
        light = [pixels[i] for i in order[split:]]
        fg, bg = mean(dark), mean(light)
        total = error(dark, fg) + error(light, bg)
        if best is None or total < best[0]:
            best = (total, sum(1 << i for i in order[:split]), fg, bg)
    return cover_preview._sextant(best[1]), best[2], best[3]


def test_block_cells_match_the_brute_force_search():
    rng = random.Random(1)
    cells = [
        [tuple(rng.randrange(256) for _ in range(3)) for _ in range(6)]
        for _ in range(20000)
    ]
    cells += [[(rng.choice((0, 255)),) * 3 for _ in range(6)] for _ in range(2000)]
    cells += [[(7, 7, 7)] * 6, [(0, 0, 0)] * 3 + [(255, 255, 255)] * 3]
    for pixels in cells:
        assert cover_preview._cell(pixels) == _cell_by_brute_force(pixels), pixels
