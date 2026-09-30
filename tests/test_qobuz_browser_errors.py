from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from streamrip.rip import qobuz_token_capture as capture


@pytest.fixture
def browser_flow(monkeypatch):
    browser = AsyncMock()
    context = browser.new_context.return_value
    page = context.new_page.return_value
    page.evaluate.return_value = '{"id": 123, "token": "test-token"}'
    manager = AsyncMock()
    manager.__aenter__.return_value.chromium.launch.return_value = browser
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: manager)
    launch = AsyncMock(return_value=(browser, "test-browser"))
    monkeypatch.setattr(capture, "_launch_installed_chromium", launch)
    return manager, launch, browser, page


@pytest.mark.parametrize(
    "stage", ["startup", "launch", "context", "page", "navigation"]
)
async def test_browser_setup_errors_allow_manual_fallback(browser_flow, stage):
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
    if stage in ("context", "page", "navigation"):
        browser.close.assert_awaited_once()


async def test_downloaded_browser_launch_error_allows_manual_fallback(
    monkeypatch, browser_flow
):
    manager, launch, _, _ = browser_flow
    launch.return_value = (None, None)
    monkeypatch.setattr(capture.Confirm, "ask", MagicMock(return_value=True))
    monkeypatch.setattr(capture, "_download_playwright_chromium", AsyncMock())
    manager.__aenter__.return_value.chromium.launch.side_effect = PlaywrightError(
        "Browser launch failed"
    )
    with pytest.raises(capture.QobuzTokenCaptureError, match="Browser launch failed"):
        await capture.capture_qobuz_auth_token_via_browser()


async def test_existing_capture_error_is_preserved(browser_flow):
    _, launch, _, _ = browser_flow
    error = capture.QobuzTokenCaptureError("Already handled")
    launch.side_effect = error
    with pytest.raises(capture.QobuzTokenCaptureError) as raised:
        await capture.capture_qobuz_auth_token_via_browser()
    assert raised.value is error


async def test_polling_retries_after_navigation_error(monkeypatch, browser_flow):
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
