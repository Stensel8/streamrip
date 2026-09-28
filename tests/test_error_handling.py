import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.media.album import Album
from streamrip.media.playlist import Playlist


class TestErrorHandling:
    """Test error handling in playlist and album downloads."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefer_explicit", [False, True])
    async def test_playlist_handles_failed_track(self, prefer_explicit):
        """Test that a playlist download continues even if one track fails."""
        mock_config = MagicMock()
        mock_config.session.metadata.prefer_explicit = prefer_explicit
        mock_client = MagicMock()

        mock_track_success = MagicMock()
        mock_track_success.resolve = AsyncMock(return_value=MagicMock())
        mock_track_success.resolve.return_value.rip = AsyncMock()

        mock_track_failure = MagicMock()
        mock_track_failure.resolve = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", "", 0)
        )

        playlist = Playlist(
            name="Test Playlist",
            config=mock_config,
            client=mock_client,
            tracks=[mock_track_success, mock_track_failure],
        )

        await playlist.download()

        mock_track_success.resolve.assert_called_once()
        mock_track_success.resolve.return_value.rip.assert_called_once()
        mock_track_failure.resolve.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefer_explicit", [False, True])
    async def test_album_handles_failed_track(self, prefer_explicit):
        """Test that an album download continues even if one track fails."""
        mock_config = MagicMock()
        mock_config.session.metadata.prefer_explicit = prefer_explicit
        mock_db = MagicMock()
        mock_meta = MagicMock()
        mock_meta.info.container = "FLAC"
        mock_meta.info.bit_depth = None
        mock_meta.info.sampling_rate = None

        # Create a list of mock tracks - one will succeed, one will fail
        mock_track_success = MagicMock()
        mock_track_success.resolve = AsyncMock(return_value=MagicMock())
        mock_track_success.resolve.return_value.rip = AsyncMock()

        # This track will raise a JSONDecodeError when resolved
        mock_track_failure = MagicMock()
        mock_track_failure.resolve = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", "", 0)
        )

        album = Album(
            meta=mock_meta,
            config=mock_config,
            tracks=[mock_track_success, mock_track_failure],
            folder="/test/folder",
            db=mock_db,
        )

        await album.download()

        mock_track_success.resolve.assert_called_once()
        mock_track_success.resolve.return_value.rip.assert_called_once()
        mock_track_failure.resolve.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_rip_handles_failed_media(self):
        """Test that the Main.rip method handles failed media items."""
        from streamrip.rip.main import Main

        mock_config = MagicMock()

        mock_config.session.downloads.requests_per_minute = 0
        mock_config.session.database.downloads_enabled = False
        mock_config.session.database.failed_downloads_enabled = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(mock_config)

            mock_media_success = MagicMock()
            mock_media_success.rip = AsyncMock()

            mock_media_failure = MagicMock()
            mock_media_failure.rip = AsyncMock(
                side_effect=Exception("Media download failed")
            )

            main.media = [mock_media_success, mock_media_failure]

            await main.rip()

            mock_media_success.rip.assert_called_once()
            mock_media_failure.rip.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_resolve_handles_a_failing_item(self, caplog):
        """One URL that fails to resolve must not stop the others."""
        from streamrip.rip.main import Main

        mock_config = MagicMock()
        mock_config.session.database.downloads_enabled = False
        mock_config.session.database.failed_downloads_enabled = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(mock_config)

            ok = MagicMock()
            ok.resolve = AsyncMock(return_value="album")
            broken = MagicMock()
            broken.id = "123"
            broken.resolve = AsyncMock(side_effect=KeyError("tracks"))
            main.pending = [broken, ok]

            await main.resolve()

            assert main.media == ["album"]
            assert "Error resolving 123: KeyError" in caplog.text

    @pytest.mark.asyncio
    async def test_search_page_without_results_is_not_a_crash(self):
        """A search can return a page with nothing in it; that's no results."""
        from streamrip.rip.main import Main

        mock_config = MagicMock()
        mock_config.session.database.downloads_enabled = False
        mock_config.session.database.failed_downloads_enabled = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(mock_config)
            main.clients["deezer"].search = AsyncMock(return_value=[{"data": []}])

            await main.search_take_first("deezer", "track", "nothing matches")

            assert main.pending == []
