from unittest.mock import AsyncMock, MagicMock, patch


class TestErrorHandling:
    """How Main reports, resolves and searches when something fails or is empty."""

    async def test_main_rip_summary_counts_tracks_not_items(self, caplog):
        """One artist is one item, but hundreds of tracks: say the latter."""
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
            patch("streamrip.rip.main.SpotifyClient"),
        ):
            main = Main(mock_config)

            async def rip_discography():
                for n in range(40):
                    main.database.set_downloaded("qobuz", str(n))
                main.database.set_failed("qobuz", "track", "rider")
                main.database.skipped_now += 3

            artist = MagicMock()
            artist.rip = rip_discography
            main.media = [artist]

            await main.rip()

            assert (
                "Download completed: 40 tracks downloaded, 3 already downloaded, "
                "1 failed" in caplog.text
            )
            assert "items downloaded" not in caplog.text

    async def test_main_rip_with_nothing_resolved_says_nothing(self, caplog):
        """A search written to a file resolves nothing: no "Download completed:
        0 tracks downloaded" after it."""
        from streamrip.rip.main import Main

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
            patch("streamrip.rip.main.SpotifyClient"),
        ):
            mock_config = MagicMock()
            mock_config.session.database.downloads_enabled = False
            mock_config.session.database.failed_downloads_enabled = False
            main = Main(mock_config)
            await main.rip()

        assert "Download completed" not in caplog.text

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
            patch("streamrip.rip.main.SpotifyClient"),
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
            patch("streamrip.rip.main.SpotifyClient"),
        ):
            main = Main(mock_config)
            main.clients["deezer"].search = AsyncMock(return_value=[{"data": []}])

            await main.search_take_first("deezer", "track", "nothing matches")

            assert main.pending == []
