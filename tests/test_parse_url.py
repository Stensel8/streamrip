from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from streamrip.client import new_session
from streamrip.config import Config
from streamrip.rip.parse_url import (
    DeezerDynamicURL,
    DeezerFavoriteURL,
    GenericURL,
    QobuzInterpreterURL,
    SoundcloudURL,
    parse_url,
)


@pytest.mark.parametrize(
    ("url", "source", "media_type", "item_id"),
    [
        (
            "https://www.qobuz.com/fr-fr/album/bizarre-ride-ii-the-pharcyde-the-pharcyde/0066991040005",
            "qobuz",
            "album",
            "0066991040005",
        ),
        ("https://tidal.com/browse/track/3083287", "tidal", "track", "3083287"),
        ("https://www.deezer.com/track/4195713", "deezer", "track", "4195713"),
        # The share sheet appends "/u", which would otherwise be taken as the
        # item id and 404 against the API.
        ("https://tidal.com/album/152697662/u", "tidal", "album", "152697662"),
        ("https://tidal.com/album/152697662/u/", "tidal", "album", "152697662"),
        ("https://tidal.com/browse/album/152697662/u", "tidal", "album", "152697662"),
    ],
)
def test_a_service_url_gives_its_source_type_and_id(url, source, media_type, item_id):
    result = parse_url(url)

    assert isinstance(result, GenericURL)
    assert result.source == source
    assert result.match.groups() == (source, media_type, item_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://open.tidal.com/track/3083287",
        "https://play.qobuz.com/album/0066991040005",
        "https://listen.tidal.com/track/3083287",
        "https://www.qobuz.com/us-en/album/name/id123456",
        "https://www.qobuz.com/gb-en/album/name/id123456",
        "https://www.deezer.com/en/track/4195713",
        "https://www.deezer.com/fr/track/4195713",
    ],
)
def test_other_subdomains_and_language_codes_are_accepted(url):
    assert isinstance(parse_url(url), GenericURL)


@pytest.mark.parametrize(
    "url",
    [
        "https://dzr.page.link/SnV6hCyHihkmCCwUA",
        # Old Firebase share links and the current link.deezer.com ones.
        "https://deezer.page.link/qfomS8twgYA35mgQ7",
        "https://dzr.page.link/qfomS8twgYA35mgQ7",
        "https://link.deezer.com/s/30hqmqELWXacckhxPdGqX",
    ],
)
def test_deezer_share_links(url):
    result = parse_url(url)

    assert isinstance(result, DeezerDynamicURL)
    assert result.source == "deezer"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.deezer.com/en/track/4195713?host=0", ("track", "4195713")),
        ("https://www.deezer.com/album/723513301", ("album", "723513301")),
        ("https://www.deezer.com/pt-br/playlist/908622995", ("playlist", "908622995")),
    ],
)
def test_the_id_is_read_from_the_url_a_deezer_share_link_redirects_to(url, expected):
    assert DeezerDynamicURL.standard_link_re.search(url).groups() == expected


def test_deezer_loved_tracks_url():
    result = parse_url("https://www.deezer.com/fr/profile/1234567/loved")

    assert isinstance(result, DeezerFavoriteURL)
    assert result.match.group(1) == "1234567"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "not a url",
        "https://spotify.com/track/123456",  # not open.spotify.com
        "https://tidal.com/invalid/3083287",  # Invalid media type
    ],
)
def test_invalid_urls_do_not_parse(url):
    assert parse_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/artist-name/track-name",
        "https://soundcloud.com/artist-name/sets/playlist-name",
    ],
)
def test_soundcloud_url(url):
    result = parse_url(url)

    assert isinstance(result, SoundcloudURL)
    assert result.source == "soundcloud"


async def test_a_deezer_dynamic_link_becomes_the_album_it_leads_to():
    with patch(
        "streamrip.rip.parse_url.DeezerDynamicURL._extract_info_from_dynamic_link"
    ) as extract:
        extract.return_value = ("album", "12345")
        client = AsyncMock()
        client.source = "deezer"

        pending = await parse_url(
            "https://dzr.page.link/SnV6hCyHihkmCCwUA"
        ).into_pending(client, AsyncMock(), AsyncMock())

    assert pending.__class__.__name__ == "PendingAlbum"
    assert pending.id == "12345"


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
