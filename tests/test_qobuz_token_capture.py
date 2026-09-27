from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.rip.qobuz_token_capture import (
    QobuzTokenCaptureError,
    _capture_qobuz_auth_token_async,
    _install_chromium,
)


@pytest.mark.asyncio
async def test_install_chromium_succeeds():
    proc = AsyncMock()
    proc.communicate.return_value = (b"", None)
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await _install_chromium()  # should not raise


@pytest.mark.asyncio
async def test_install_chromium_failure_raises():
    proc = AsyncMock()
    proc.communicate.return_value = (b"network error", None)
    proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(QobuzTokenCaptureError, match="Could not download"):
            await _install_chromium()


@pytest.mark.asyncio
async def test_missing_chromium_is_installed_automatically_then_retried():
    """The first launch fails the way Playwright does when the browser isn't
    downloaded yet; streamrip should install it and retry, not just give up.
    """
    browser = AsyncMock()
    context = AsyncMock()
    page = MagicMock()
    page.on = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()

    browser.new_context.return_value = context
    context.new_page.return_value = page

    chromium = MagicMock()
    chromium.launch = AsyncMock(
        side_effect=[
            Exception(
                "BrowserType.launch: Executable doesn't exist ... "
                "Run 'playwright install' to download new browsers."
            ),
            browser,
        ]
    )

    playwright_ctx = MagicMock()
    playwright_ctx.chromium = chromium
    async_playwright_cm = AsyncMock()
    async_playwright_cm.__aenter__.return_value = playwright_ctx
    async_playwright_cm.__aexit__.return_value = False

    install_mock = AsyncMock()

    with (
        patch(
            "playwright.async_api.async_playwright",
            return_value=async_playwright_cm,
        ),
        patch("streamrip.rip.qobuz_token_capture._install_chromium", install_mock),
        pytest.raises(QobuzTokenCaptureError, match="Could not detect"),
    ):
        # timeout_s=0: falls straight through to "no login detected" once
        # the (successful, second-attempt) browser is up, without needing
        # to simulate real Qobuz login network traffic.
        await _capture_qobuz_auth_token_async(timeout_s=0)

    install_mock.assert_awaited_once()
    assert chromium.launch.await_count == 2
