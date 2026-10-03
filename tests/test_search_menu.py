"""Search UI behavior with a real Textual app and synthetic, local covers."""

import asyncio
import builtins
import io
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
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
                image_url=url,
                details=[("Released", "2026-10-03"), ("Tracks", "12")],
            )
            for i, url in enumerate(urls or (None, None, None))
        ]
    )


@asynccontextmanager
async def menu(results=None, *, size=(100, 40), **kwargs):
    app = SearchMenu(results or summaries(), "qobuz", "album", "test", **kwargs)
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


@pytest.mark.asyncio
async def test_selection_survives_filter_and_returns_original_indices():
    async with menu() as (app, pilot):
        await pilot.press("space", "slash", "s", "e", "c", "o", "n", "d", "enter")
    assert app.return_value == (0, 1)


@pytest.mark.asyncio
async def test_enter_without_marks_chooses_highlighted_result():
    async with menu() as (app, pilot):
        await pilot.press("down", "enter")
    assert app.return_value == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_cancel_never_returns_selected_downloads(key):
    async with menu() as (app, pilot):
        await pilot.press("space", key)
    assert app.return_value is None


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_no_matching_results_clears_preview_and_cannot_accept():
    async with menu() as (app, pilot):
        await pilot.press("slash")
        app.query_one(Input).value = "no such album"
        await pilot.pause()
        assert app.current is None
        assert not app.query_one(CoverPane).display
        assert "No matching results" in str(app.query_one("#details", Static).content)
        await pilot.press("enter", "enter")
        assert app.is_running


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_same_cover_is_not_replaced_when_marking_or_changing_track(monkeypatch):
    fetch = AsyncMock(return_value=png())
    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu(summaries("same", "same")) as (app, pilot):
        await pilot.pause(0.2)
        pane = app.query_one(CoverPane)
        child = pane.children[0]
        await pilot.press("space", "down")
        await pilot.pause()
        assert pane.children[0] is child
        assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_small_and_resized_layout_never_overlaps_metadata(monkeypatch):
    monkeypatch.setattr(CoverCache, "_fetch", AsyncMock(return_value=png()))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("offline"), b"not an image"])
async def test_bad_cover_leaves_details_and_selection_usable(monkeypatch, failure):
    fetch = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, Exception)
        else AsyncMock(return_value=failure)
    )
    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu(summaries("broken")) as (app, pilot):
        await pilot.pause(0.2)
        assert app.query_one(CoverPane).image is None
        assert "First Album" in str(app.query_one("#details", Static).content)
        await pilot.press("enter")
    assert app.return_value == (0,)


@pytest.mark.asyncio
async def test_missing_artwork_does_not_download_and_keeps_metadata(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(CoverCache, "_fetch", fetch)
    async with menu() as (app, pilot):
        await pilot.pause(0.2)
        assert "First Album" in str(app.query_one("#details", Static).content)
        assert app.query_one(CoverPane).image is None
        await pilot.press("enter")
    assert app.return_value == (0,)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_tries_smaller_variant_and_caches_errors_and_success(monkeypatch):
    fetch = AsyncMock(side_effect=[OSError("missing"), png()])
    cache = CoverCache()
    monkeypatch.setattr(cache, "_fetch", fetch)
    image = await cache.get(("large", "small"))
    assert image is not None
    assert await cache.get(("large", "small")) is image
    assert fetch.await_count == 2
    await cache.close()


@pytest.mark.parametrize("stream", ["stdin", "stdout"])
def test_auto_does_not_import_or_probe_image_library_for_redirected_io(
    monkeypatch, stream
):
    original = builtins.__import__

    def guard(name, *args, **kwargs):
        assert not name.startswith("textual_image")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(cover_preview.sys, "stdin", Mock(isatty=lambda: True))
    monkeypatch.setattr(cover_preview.sys, "stdout", Mock(isatty=lambda: True))
    monkeypatch.setattr(cover_preview.sys, stream, Mock(isatty=lambda: False))
    monkeypatch.setattr(builtins, "__import__", guard)
    assert cover_preview.image_widget() is None


def test_failed_terminal_probe_automatically_uses_blocks(monkeypatch):
    original = builtins.__import__

    def fail(name, *args, **kwargs):
        if name == "textual_image":
            raise OSError("terminal probe failed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(cover_preview.sys, "stdin", Mock(isatty=lambda: True))
    monkeypatch.setattr(cover_preview.sys, "stdout", Mock(isatty=lambda: True))
    monkeypatch.setattr(builtins, "__import__", fail)
    assert cover_preview.image_widget() is None


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_pixel_renderer_failure_uses_blocks(monkeypatch):
    monkeypatch.setattr(CoverCache, "_fetch", AsyncMock(return_value=png()))
    broken_widget = Mock(side_effect=RuntimeError("encoder failed"))
    async with menu(summaries("cover"), pixel_widget=broken_widget) as (app, pilot):
        await pilot.pause(0.2)
        pane = app.query_one(CoverPane)
        assert pane.query_one(BlockCover)
        assert pane.pixel_widget is None


@pytest.mark.parametrize(
    "image_fields,expected",
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


@pytest.mark.asyncio
async def test_main_queues_original_choices_only(monkeypatch):
    from streamrip.rip.main import Main

    main = Main.__new__(Main)
    main.config = Config.defaults()
    main._search = AsyncMock(return_value=summaries())
    main.add_all_by_id = AsyncMock()
    choose = AsyncMock(return_value=(2, 0))
    monkeypatch.setattr(search_menu, "choose_results", choose)
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


@pytest.mark.parametrize("protocol", ["sixel", "tgp", "unsupported"])
def test_auto_selects_only_a_supported_pixel_renderer(monkeypatch, protocol):
    import sys
    from types import ModuleType, SimpleNamespace

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
    monkeypatch.setattr(cover_preview.sys, "stdin", Mock(isatty=lambda: True))
    monkeypatch.setattr(cover_preview.sys, "stdout", Mock(isatty=lambda: True))
    assert cover_preview.image_widget() is (
        None if protocol == "unsupported" else widget
    )


@pytest.mark.asyncio
async def test_delayed_encoder_failure_falls_back_for_later_covers(monkeypatch):
    class FailingImage(Static):
        def __init__(self, image, on_error):
            super().__init__()
            self.fallback = on_error

        async def on_mount(self):
            await self.mount(self.fallback(OSError("cannot encode")))

    monkeypatch.setattr(CoverCache, "_fetch", AsyncMock(return_value=png()))
    async with menu(summaries("one", "two"), pixel_widget=FailingImage) as (app, pilot):
        await pilot.pause(0.2)
        pane = app.query_one(CoverPane)
        assert pane.pixel_widget is None
        assert pane.query_one(BlockCover).size.height > 1
        await pilot.press("down")
        await pilot.pause(0.2)
        assert isinstance(pane.children[0], BlockCover)


@pytest.mark.asyncio
async def test_cache_http_download_failure_and_close():
    from aiohttp import web

    async def cover(request):
        return web.Response(body=png(), content_type="image/png")

    app = web.Application()
    app.router.add_get("/cover", cover)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site.port}"
    cache = CoverCache()
    try:
        assert await cache.get((url + "/missing", url + "/cover")) is not None
        assert cache.images[url + "/missing"] is None
        session = cache.session
    finally:
        await cache.close()
        await runner.cleanup()
    assert session.closed


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_enter_includes_current_result_like_previous_linux_menu():
    async with menu() as (app, pilot):
        await pilot.press("space", "down", "enter")
    assert app.return_value == (0, 1)


@pytest.mark.asyncio
async def test_real_sixel_adapter_restores_last_row_and_handles_encoder_errors(
    monkeypatch,
):
    from rich.control import Control

    pixel = cover_preview._sixel_widget()
    monkeypatch.setattr(CoverCache, "_fetch", AsyncMock(return_value=png()))
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
