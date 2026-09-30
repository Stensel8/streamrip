"""Tidal hi-res needs ffmpeg: say so before downloading, not per track."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import FFmpegNotFoundError, TrackDownloadFailedError
from streamrip.media.track import MAX_DOWNLOAD_ATTEMPTS, Track
from streamrip.rip.cli import rip
from streamrip.rip.main import Main
from streamrip.utils.ffmpeg_utils import ffmpeg_missing_message


def _main(tidal_logged_in=True, quality=3):
    config = Config.defaults()
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.tidal.quality = quality
    main = Main(config)
    main.clients["tidal"].logged_in = tidal_logged_in
    main.media = []
    return main


@pytest.mark.parametrize(
    "logged_in, quality, ffmpeg, aborts",
    [
        (True, 3, None, True),
        (True, 3, "/usr/bin/ffmpeg", False),  # ffmpeg present
        (True, 2, None, False),  # CD quality needs no ffmpeg
        (False, 3, None, False),  # Tidal is not used in this run
    ],
)
async def test_rip_checks_ffmpeg_only_for_tidal_hires(
    logged_in, quality, ffmpeg, aborts
):
    main = _main(logged_in, quality)
    with patch("streamrip.rip.main.find_ffmpeg", return_value=ffmpeg):
        if aborts:
            with pytest.raises(FFmpegNotFoundError, match="No ffmpeg installation"):
                await main.rip()
        else:
            await main.rip()


async def test_rip_does_not_touch_media_when_aborting():
    main = _main()
    item = MagicMock(rip=AsyncMock())
    main.media = [item]
    with patch("streamrip.rip.main.find_ffmpeg", return_value=None):
        with pytest.raises(FFmpegNotFoundError):
            await main.rip()
    item.rip.assert_not_called()


def test_cli_prints_the_message_and_exits_non_zero(tmp_path):
    cfg = tmp_path / "config.toml"
    set_user_defaults(str(cfg))
    with (
        patch.object(Main, "add_all", AsyncMock()),
        patch.object(Main, "resolve", AsyncMock()),
        patch.object(
            Main,
            "rip",
            AsyncMock(side_effect=FFmpegNotFoundError(ffmpeg_missing_message())),
        ),
    ):
        result = CliRunner().invoke(
            rip, ["--config-path", str(cfg), "url", "https://tidal.com/album/1"]
        )
    assert result.exit_code == 1
    assert "No ffmpeg installation found" in result.output
    # Text like "[ffmpeg]" or "[tidal]" must not be eaten as rich markup.
    assert "[tidal]" in result.output


async def test_missing_ffmpeg_is_not_retried():
    track = MagicMock()
    track._skip_lossy_duplicate = False
    track.config.session.downloads.max_connections = 6
    track.downloadable.size = AsyncMock(return_value=0)
    track.downloadable.download = AsyncMock(side_effect=FFmpegNotFoundError("no"))
    track.downloadable.source = "tidal"
    track.config.session.cli.progress_bars = False
    track.is_single = False
    track.download_path = "/nonexistent/track.flac"
    track.meta.title = "T"
    track.meta.tracknumber = 1
    track.meta.info.id = "1"
    with (
        patch("streamrip.media.track.format_quality", return_value="FLAC"),
        patch("streamrip.media.track.asyncio.sleep", AsyncMock()) as sleep,
    ):
        with pytest.raises(TrackDownloadFailedError):
            await Track.download(track)
    assert track.downloadable.download.await_count == 1
    sleep.assert_not_called()
    assert MAX_DOWNLOAD_ATTEMPTS > 1
