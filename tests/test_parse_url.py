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
        ("https://www.deezer.com/en/track/4195713", "deezer", "track", "4195713"),
        # The share sheet appends "/u", which was taken as the item id.
        ("https://tidal.com/album/152697662/u", "tidal", "album", "152697662"),
        # So were the pages under an item.
        ("https://tidal.com/browse/album/123/credits", "tidal", "album", "123"),
        ("https://www.deezer.com/en/artist/27/top_track", "deezer", "artist", "27"),
        ("https://listen.tidal.com/track/3083287", "tidal", "track", "3083287"),
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
        "https://tidal.com/invalid/3083287",
    ],
)
def test_invalid_urls_do_not_parse(url):
    assert parse_url(url) is None


SC_TRACK = "https://soundcloud.com/artist-name/track-name"


@pytest.mark.parametrize(
    "url", [SC_TRACK, "https://m.soundcloud.com/artist-name/track-name"]
)
def test_soundcloud_urls_resolve_as_the_plain_host(url):
    result = parse_url(url)

    assert isinstance(result, SoundcloudURL)
    assert result.url == SC_TRACK


@pytest.mark.parametrize(
    "url",
    [
        "https://dzr.page.link/qfomS8twgYA35mgQ7",  # the old Firebase links
        "https://link.deezer.com/s/30hqmqELWXacckhxPdGqX",
    ],
)
def test_deezer_share_links(url):
    assert isinstance(parse_url(url), DeezerDynamicURL)


async def test_a_deezer_share_link_becomes_the_album_it_leads_to():
    with patch(
        "streamrip.rip.parse_url.DeezerDynamicURL._extract_info_from_dynamic_link",
        return_value=("album", "12345"),
    ):
        client = AsyncMock()
        client.source = "deezer"

        pending = await parse_url(
            "https://dzr.page.link/SnV6hCyHihkmCCwUA"
        ).into_pending(client, AsyncMock(), AsyncMock())

    assert pending.__class__.__name__ == "PendingAlbum"
    assert pending.id == "12345"


@pytest.mark.parametrize(
    ("url", "kind", "item_id"),
    [
        (
            "https://www.deezer.com/fr/profile/1234567/loved",
            PendingPlaylist,
            "favorites:1234567",
        ),
        (
            "https://www.deezer.com/en-gb/profile/1234567/albums",
            PendingLabel,
            "favorites:1234567",
        ),
        (
            "https://listen.tidal.com/my-collection/tracks",
            PendingPlaylist,
            "favorites:tracks",
        ),
    ],
)
async def test_favorites_are_a_playlist_of_tracks_or_a_label_of_albums(
    url, kind, item_id
):
    result = parse_url(url)
    assert isinstance(result, DeezerFavoriteURL | TidalFavoriteURL)
    pending = await result.into_pending(MagicMock(), Config.defaults(), MagicMock())
    assert isinstance(pending, kind)
    assert pending.id == item_id


def _page(status=200, body="getSimilarArtist('12345')"):
    response = MagicMock(status=status)
    response.text = AsyncMock(return_value=body)
    response.__aenter__.return_value = response
    return response


@pytest.mark.login
async def test_the_interpreter_page_is_fetched_without_the_login():
    config = Config.defaults()
    sessions = []

    async with new_session() as authenticated:
        authenticated.headers["X-User-Auth-Token"] = "test-token"
        authenticated.cookie_jar.update_cookies({"account": "test-cookie"})
        client = MagicMock(session=authenticated)

        def get(session, url, **kwargs):
            sessions.append(session)
            assert session is not authenticated
            assert "X-User-Auth-Token" not in session.headers
            assert not session.cookie_jar
            assert kwargs["allow_redirects"] is False
            return _page()

        with patch("aiohttp.ClientSession.get", autospec=True, side_effect=get):
            parsed = parse_url(INTERPRETER)
            assert isinstance(parsed, QobuzInterpreterURL)
            pending = await parsed.into_pending(client, config, MagicMock())

        assert pending.id == "12345"
        assert sessions[0].closed
        assert not authenticated.closed


async def test_an_interpreter_url_with_a_numeric_id_needs_no_page_fetch():
    parsed = parse_url("https://www.qobuz.com/us-en/interpreter/miles-davis/12345")
    with patch("streamrip.rip.parse_url.new_session", side_effect=AssertionError):
        pending = await parsed.into_pending(MagicMock(), Config.defaults(), MagicMock())
    assert pending.id == "12345"


@pytest.mark.parametrize(
    ("status", "error"),
    [(302, "redirected"), (404, "404"), (200, "Unable to extract artist id")],
    ids=["redirect", "not found", "no artist on the page"],
)
async def test_interpreter_fetch_errors_close_the_public_session(status, error):
    # Even a redirect body that contains an id must not be accepted.
    response = _page(status, "" if status == 200 else "getSimilarArtist('1')")
    response.headers = {"Location": "http://www.qobuz.com/"}
    if status == 404:
        response.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(), (), status=404
        )
    sessions = []

    def get(session, url, **kwargs):
        sessions.append(session)
        return response

    with (
        patch("aiohttp.ClientSession.get", autospec=True, side_effect=get),
        pytest.raises(Exception, match=error),
    ):
        await QobuzInterpreterURL.extract_interpreter_url(INTERPRETER)
    assert sessions[0].closed
