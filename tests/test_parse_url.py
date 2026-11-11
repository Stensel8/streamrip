from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from streamrip.client import new_session
from streamrip.config import Config
from streamrip.media import PendingLabel, PendingPlaylist
from streamrip.rip.parse_url import (
    DeezerDynamicURL,
    DeezerFavoriteURL,
    GenericURL,
    QobuzInterpreterURL,
    SoundcloudURL,
    TidalFavoriteURL,
    parse_url,
)

INTERPRETER = (
    "https://www.qobuz.com/us-en/interpreter/miles-davis/download-streaming-albums"
)

# --- parsing ------------------------------------------------------------------


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
        # Other subdomains and language codes.
        ("https://open.tidal.com/track/3083287", "tidal", "track", "3083287"),
        ("https://listen.tidal.com/track/3083287", "tidal", "track", "3083287"),
        (
            "https://play.qobuz.com/album/0066991040005",
            "qobuz",
            "album",
            "0066991040005",
        ),
        (
            "https://www.qobuz.com/us-en/album/name/id123456",
            "qobuz",
            "album",
            "id123456",
        ),
        ("https://www.deezer.com/en/track/4195713", "deezer", "track", "4195713"),
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
        "https://example.com",
        "not a url",
        "https://spotify.com/track/123456",  # not open.spotify.com
        "https://tidal.com/invalid/3083287",  # Invalid media type
    ],
)
def test_invalid_urls_do_not_parse(url):
    assert parse_url(url) is None


SC_TRACK = "https://soundcloud.com/artist-name/track-name"
SC_SET = "https://soundcloud.com/artist-name/sets/playlist-name"


@pytest.mark.parametrize(
    ("url", "resolved"),
    [
        (SC_TRACK, SC_TRACK),
        (SC_SET, SC_SET),
        ("https://www.soundcloud.com/artist-name/track-name", SC_TRACK),
        ("https://m.soundcloud.com/artist-name/track-name", SC_TRACK),
        ("http://soundcloud.com/artist-name/track-name", SC_TRACK),
    ],
)
def test_soundcloud_urls_resolve_as_the_plain_host(url, resolved):
    result = parse_url(url)

    assert isinstance(result, SoundcloudURL)
    assert result.source == "soundcloud"
    assert result.url == resolved


# --- Deezer share links -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
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


# --- favorites ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind", "item_id"),
    [
        (
            "https://www.deezer.com/fr/profile/1234567/loved",
            "playlist",
            "favorites:1234567",
        ),
        # Without a language in the path, as Deezer's own links often are.
        (
            "https://www.deezer.com/profile/1234567/loved",
            "playlist",
            "favorites:1234567",
        ),
        (
            "https://www.deezer.com/en-gb/profile/1234567/albums",
            "label",
            "favorites:1234567",
        ),
        (
            "https://listen.tidal.com/my-collection/tracks",
            "playlist",
            "favorites:tracks",
        ),
        ("https://tidal.com/browse/my-collection/albums", "label", "favorites:albums"),
    ],
)
async def test_favorites_urls_are_a_playlist_of_tracks_or_a_label_of_albums(
    url, kind, item_id
):
    result = parse_url(url)
    assert isinstance(result, DeezerFavoriteURL | TidalFavoriteURL)
    pending = await result.into_pending(MagicMock(), Config.defaults(), MagicMock())
    assert isinstance(pending, PendingPlaylist if kind == "playlist" else PendingLabel)
    assert pending.id == item_id


# --- Qobuz interpreter pages --------------------------------------------------


@pytest.mark.login
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_interpreter_fetch_isolated_from_authenticated_session(
    scheme, verify_ssl
):
    url = INTERPRETER.replace("https", scheme, 1)
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
            assert request_url == INTERPRETER  # never plain http
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


@pytest.mark.parametrize(
    ("status", "error"),
    [
        *((status, "redirected") for status in (301, 302, 303, 307, 308)),
        (404, "404"),
        (200, "Unable to extract artist id"),  # a page without one
    ],
)
async def test_interpreter_fetch_errors_close_public_session(status, error):
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

    with (
        patch("aiohttp.ClientSession.get", autospec=True, side_effect=get),
        pytest.raises(Exception, match=error),
    ):
        await QobuzInterpreterURL.extract_interpreter_url(INTERPRETER)
    assert len(sessions) == 1
    assert sessions[0].closed
