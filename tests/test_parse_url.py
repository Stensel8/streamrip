import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from streamrip.client import new_session
from streamrip.config import Config
from streamrip.rip.parse_url import (
    DeezerDynamicURL,
    GenericURL,
    QobuzInterpreterURL,
    SoundcloudURL,
    parse_url,
)


class TestParseURL(unittest.TestCase):
    def test_deezer_dynamic_url(self):
        """Test that Deezer dynamic URLs are matched correctly."""
        url = "https://dzr.page.link/SnV6hCyHihkmCCwUA"
        result = parse_url(url)

        self.assertIsNotNone(result)
        self.assertIsInstance(result, DeezerDynamicURL)
        self.assertEqual(result.source, "deezer")

    def test_qobuz_album_url(self):
        """Test that Qobuz album URLs are matched correctly."""
        url = "https://www.qobuz.com/fr-fr/album/bizarre-ride-ii-the-pharcyde-the-pharcyde/0066991040005"
        result = parse_url(url)

        self.assertIsNotNone(result)
        self.assertIsInstance(result, GenericURL)
        self.assertEqual(result.source, "qobuz")

        # Verify the regex match groups
        groups = result.match.groups()
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0], "qobuz")  # source
        self.assertEqual(groups[1], "album")  # media_type
        self.assertEqual(groups[2], "0066991040005")  # item_id

    def test_tidal_track_url(self):
        """Test that Tidal track URLs are matched correctly."""
        url = "https://tidal.com/browse/track/3083287"
        result = parse_url(url)

        self.assertIsNotNone(result)
        self.assertIsInstance(result, GenericURL)
        self.assertEqual(result.source, "tidal")

        # Verify the regex match groups
        groups = result.match.groups()
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0], "tidal")  # source
        self.assertEqual(groups[1], "track")  # media_type
        self.assertEqual(groups[2], "3083287")  # item_id

    def test_tidal_share_url_with_u_suffix(self):
        """Test that Tidal share links ending in /u parse to the real item id.

        The share sheet appends "/u", which would otherwise be taken as the
        item id and 404 against the API.
        """
        for url in (
            "https://tidal.com/album/152697662/u",
            "https://tidal.com/album/152697662/u/",
            "https://tidal.com/browse/album/152697662/u",
        ):
            with self.subTest(url=url):
                result = parse_url(url)

                self.assertIsNotNone(result)
                self.assertIsInstance(result, GenericURL)
                self.assertEqual(result.source, "tidal")

                groups = result.match.groups()
                self.assertEqual(groups[1], "album")  # media_type
                self.assertEqual(groups[2], "152697662")  # item_id

    def test_deezer_share_links(self):
        """Old Firebase share links and the current link.deezer.com ones."""
        for url in (
            "https://deezer.page.link/qfomS8twgYA35mgQ7",
            "https://dzr.page.link/qfomS8twgYA35mgQ7",
            "https://link.deezer.com/s/30hqmqELWXacckhxPdGqX",
        ):
            with self.subTest(url=url):
                result = parse_url(url)
                self.assertIsInstance(result, DeezerDynamicURL)
                self.assertEqual(result.source, "deezer")

    def test_deezer_standard_link_regex(self):
        """The id is read from the URL a share link redirects to."""
        regex = DeezerDynamicURL.standard_link_re
        for url, expected in (
            ("https://www.deezer.com/en/track/4195713?host=0", ("track", "4195713")),
            ("https://www.deezer.com/album/723513301", ("album", "723513301")),
            (
                "https://www.deezer.com/pt-br/playlist/908622995",
                ("playlist", "908622995"),
            ),
        ):
            with self.subTest(url=url):
                self.assertEqual(regex.search(url).groups(), expected)

    def test_deezer_loved_tracks_url(self):
        from streamrip.rip.parse_url import DeezerFavoriteURL

        result = parse_url("https://www.deezer.com/fr/profile/1234567/loved")
        self.assertIsInstance(result, DeezerFavoriteURL)
        self.assertEqual(result.match.group(1), "1234567")

    def test_deezer_track_url(self):
        """Test that Deezer track URLs are matched correctly."""
        url = "https://www.deezer.com/track/4195713"
        result = parse_url(url)

        self.assertIsNotNone(result)
        self.assertIsInstance(result, GenericURL)
        self.assertEqual(result.source, "deezer")

        # Verify the regex match groups
        groups = result.match.groups()
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0], "deezer")  # source
        self.assertEqual(groups[1], "track")  # media_type
        self.assertEqual(groups[2], "4195713")  # item_id

    def test_invalid_url(self):
        """Test that invalid URLs return None."""
        urls = [
            "https://example.com",
            "not a url",
            "https://spotify.com/track/123456",  # Unsupported source
            "https://tidal.com/invalid/3083287",  # Invalid media type
        ]

        for url in urls:
            result = parse_url(url)
            self.assertIsNone(result, f"URL should not parse: {url}")

    def test_alternate_url_formats(self):
        """Test various URL formats that should be valid."""
        # Test with different domain prefixes
        url1 = "https://open.tidal.com/track/3083287"
        url2 = "https://play.qobuz.com/album/0066991040005"
        url3 = "https://listen.tidal.com/track/3083287"

        for url in [url1, url2, url3]:
            result = parse_url(url)
            self.assertIsNotNone(result, f"Should parse URL: {url}")
            self.assertIsInstance(result, GenericURL)

    def test_url_with_language_code(self):
        """Test URLs with different language codes."""
        urls = [
            "https://www.qobuz.com/us-en/album/name/id123456",
            "https://www.qobuz.com/gb-en/album/name/id123456",
            "https://www.deezer.com/en/track/4195713",
            "https://www.deezer.com/fr/track/4195713",
        ]

        for url in urls:
            result = parse_url(url)
            self.assertIsNotNone(result, f"Should parse URL: {url}")
            self.assertIsInstance(result, GenericURL)

    def test_soundcloud_url(self):
        """Test that Soundcloud URLs are matched correctly."""
        urls = [
            "https://soundcloud.com/artist-name/track-name",
            "https://soundcloud.com/artist-name/sets/playlist-name",
        ]

        for url in urls:
            result = parse_url(url)
            self.assertIsNotNone(result, f"Should parse URL: {url}")
            self.assertIsInstance(result, SoundcloudURL)
            self.assertEqual(result.source, "soundcloud")


class TestDeezerDynamicURL(unittest.TestCase):
    @patch("streamrip.rip.parse_url.DeezerDynamicURL._extract_info_from_dynamic_link")
    def test_into_pending_album(self, mock_extract):
        """Test conversion of Deezer dynamic URL to a PendingAlbum."""
        import asyncio

        async def run_test():
            url = "https://dzr.page.link/SnV6hCyHihkmCCwUA"
            result = parse_url(url)

            # Mock the extract method to return album type and ID
            mock_extract.return_value = ("album", "12345")

            # Mock the client, config, db
            mock_client = AsyncMock()
            mock_client.source = "deezer"
            mock_config = AsyncMock()
            mock_db = AsyncMock()

            # Call into_pending
            pending = await result.into_pending(mock_client, mock_config, mock_db)

            # Verify the correct pending type was created
            self.assertEqual(pending.__class__.__name__, "PendingAlbum")
            self.assertEqual(pending.id, "12345")

        # Run the coroutine
        asyncio.run(run_test())


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_interpreter_fetch_isolated_from_authenticated_session(
    scheme, verify_ssl
):
    url = f"{scheme}://www.qobuz.com/us-en/interpreter/miles-davis/download-streaming-albums"
    config = Config.defaults()
    config.session.downloads.verify_ssl = verify_ssl
    response = MagicMock(status=200)
    response.text = AsyncMock(return_value="getSimilarArtist('12345')")
    response.__aenter__.return_value = response
    sessions = []

    async with new_session() as authenticated:
        authenticated.headers["X-User-Auth-Token"] = "test-token"
        authenticated.headers["X-App-Id"] = "test-app"
        authenticated.cookie_jar.update_cookies({"account": "test-cookie"})
        client = MagicMock(session=authenticated)

        def get(session, request_url, **kwargs):
            sessions.append(session)
            assert session is not authenticated
            assert "X-User-Auth-Token" not in session.headers
            assert "X-App-Id" not in session.headers
            assert not session.cookie_jar
            assert request_url == url.replace("http://", "https://", 1)
            assert kwargs["allow_redirects"] is False
            return response

        with (
            patch("aiohttp.ClientSession.get", autospec=True, side_effect=get),
            patch("streamrip.rip.parse_url.new_session", wraps=new_session) as make,
        ):
            parsed = parse_url(url)
            assert isinstance(parsed, QobuzInterpreterURL)
            pending = await parsed.into_pending(client, config, MagicMock())

        assert pending.id == "12345"
        assert pending.client is client
        make.assert_called_once_with(verify_ssl=verify_ssl)
        assert len(sessions) == 1
        assert sessions[0].closed
        assert not authenticated.closed
        assert authenticated.headers["X-User-Auth-Token"] == "test-token"


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_interpreter_numeric_id_needs_no_page_fetch(scheme):
    parsed = parse_url(f"{scheme}://www.qobuz.com/us-en/interpreter/miles-davis/12345")
    with patch("streamrip.rip.parse_url.new_session") as make:
        pending = await parsed.into_pending(MagicMock(), Config.defaults(), MagicMock())
    assert pending.id == "12345"
    make.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 404, 200])
async def test_interpreter_fetch_errors_close_public_session(status):
    response = MagicMock(status=status)
    response.headers = {
        "Location": "http://www.qobuz.com/us-en/interpreter/miles-davis/12345"
    }
    # Even a redirect body containing an ID must not be accepted.
    response.text = AsyncMock(
        return_value="" if status == 200 else "getSimilarArtist('1')"
    )
    response.__aenter__.return_value = response
    if status == 404:
        response.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(), (), status=404
        )
    sessions = []

    def get(session, url, **kwargs):
        sessions.append(session)
        assert kwargs["allow_redirects"] is False
        return response

    error = (
        "Unable to extract artist id"
        if status == 200
        else "404"
        if status == 404
        else "redirected"
    )
    with (
        patch("aiohttp.ClientSession.get", autospec=True, side_effect=get),
        pytest.raises(Exception, match=error),
    ):
        await QobuzInterpreterURL.extract_interpreter_url(
            "https://www.qobuz.com/us-en/interpreter/miles-davis/download-streaming-albums"
        )
    assert len(sessions) == 1
    assert sessions[0].closed
    if status != 200:
        response.text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
