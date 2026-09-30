"""streamrip needs ffmpeg: say so before logging in, not per track."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import FFmpegNotFoundError, TrackDownloadFailedError
from streamrip.media.track import MAX_DOWNLOAD_ATTEMPTS, Track
from streamrip.rip.cli import rip
from streamrip.rip.main import Main


def _main():
    """Create a download session with both persistent databases disabled."""
    config = Config.defaults()
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    return Main(config)


async def test_main_refuses_to_start_without_ffmpeg():
    """Reject context entry with installation guidance when ffmpeg is missing."""
    main = _main()
    with patch("streamrip.rip.main.find_ffmpeg", return_value=None):
        with pytest.raises(FFmpegNotFoundError, match="No ffmpeg installation"):
            async with main:
                pytest.fail("Main must not be entered without ffmpeg")


async def test_main_starts_with_ffmpeg():
    """Allow context entry and return the session when ffmpeg is available."""
    main = _main()
    with patch("streamrip.rip.main.find_ffmpeg", return_value="/usr/bin/ffmpeg"):
        async with main as entered:
            assert entered is main


@pytest.mark.parametrize("command", ["url", "file", "id", "lastfm", "search"])
def test_every_login_command_checks_ffmpeg_first(tmp_path, command):
    """No command that can log in to a source gets there without ffmpeg."""
    cfg = tmp_path / "config.toml"
    set_user_defaults(str(cfg))
    urls = tmp_path / "urls.txt"
    urls.write_text("https://tidal.com/album/1\n")
    args = {
        "url": ["url", "https://tidal.com/album/1"],
        "file": ["file", str(urls)],
        "id": ["id", "qobuz", "album", "1"],
        "lastfm": ["lastfm", "https://www.last.fm/user/x/playlists/1"],
        "search": ["search", "deezer", "track", "x"],
    }[command]
    with (
        patch("streamrip.rip.main.find_ffmpeg", return_value=None),
        patch.object(Main, "get_logged_in_client", AsyncMock()) as login,
        # The console may hold on to the stdout it was created with, so check
        # what is printed instead of what CliRunner captures.
        patch("streamrip.rip.cli.console") as console,
    ):
        result = CliRunner().invoke(rip, ["--config-path", str(cfg), *args])
    assert result.exit_code == 1
    printed = "".join(str(call.args[0]) for call in console.print.call_args_list)
    assert "No ffmpeg installation found" in printed
    assert "pipx inject streamrip imageio-ffmpeg" in printed
    login.assert_not_called()


async def test_missing_ffmpeg_is_not_retried():
    """Fail after one download attempt without sleeping when ffmpeg is missing."""
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
