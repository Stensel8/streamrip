import os
from unittest.mock import Mock

import deezer
import pytest
from util import arun

from streamrip.client.deezer import DeezerClient
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError


@pytest.fixture(scope="session")
def deezer_client():
    """Integration test fixture - requires DEEZER_ARL environment variable"""
    config = Config.defaults()
    config.session.deezer.arl = os.environ.get("DEEZER_ARL", "")
    config.session.deezer.quality = 2  # FLAC
    config.session.deezer.lower_quality_if_not_available = True
    client = DeezerClient(config)
    arun(client.login())

    yield client

    arun(client.session.close())


@pytest.fixture
def mock_deezer_client():
    """Unit test fixture - mocked client for fast testing"""
    config = Config.defaults()
    config.session.deezer.arl = "test_arl"
    config.session.deezer.quality = 2
    config.session.deezer.lower_quality_if_not_available = True

    client = DeezerClient(config)
    client.client = Mock()
    client.client.gw = Mock()
    client.session = Mock()

    return client


# ===== UNIT TESTS =====


def _urls_for(*formats):
    """get_track_url stand-in serving only the given formats."""
    return lambda token, fmt: (
        f"https://cdn/x.{'flac' if fmt == 'FLAC' else 'mp3'}"
        if fmt in formats
        else None
    )


def test_deezer_fallback_logic_with_mock_data(mock_deezer_client):
    """Unit test: a tier Deezer doesn't serve falls back to the next one down"""
    mock_track_info = {
        "FILESIZE_FLAC": 0,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.side_effect = _urls_for(
        "MP3_320", "MP3_128"
    )

    downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

    # No FLAC URL, so it fell back to quality 1 (MP3_320)
    assert downloadable.quality == 1
    assert downloadable.extension == "mp3"


def test_deezer_zero_filesize_does_not_downgrade(mock_deezer_client):
    """Unit test: FILESIZE_FLAC = 0 is not "FLAC unavailable".

    Deezer often reports 0 for a format it serves. Trusting it downloaded
    those tracks as MP3 320 although the FLAC was there.
    """
    mock_deezer_client.client.gw.get_track.return_value = {
        "FILESIZE_FLAC": 0,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
    }
    mock_deezer_client.client.get_track_url.side_effect = _urls_for(
        "FLAC", "MP3_320", "MP3_128"
    )

    downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

    assert downloadable.quality == 2
    assert downloadable.extension == "flac"
    assert mock_deezer_client.client.get_track_url.call_args.args[1] == "FLAC"


def test_deezer_all_zero_filesizes_still_download(mock_deezer_client):
    """Unit test: a track with no FILESIZE at all but a URL is not skipped"""
    mock_deezer_client.client.gw.get_track.return_value = {
        "FILESIZE_FLAC": 0,
        "FILESIZE_MP3_320": 0,
        "FILESIZE_MP3_128": 0,
        "TRACK_TOKEN": "test_token",
    }
    mock_deezer_client.client.get_track_url.side_effect = _urls_for("FLAC")

    downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

    assert downloadable.quality == 2
    assert downloadable.extension == "flac"
    assert arun(downloadable.size()) == 0


def test_deezer_no_fallback_when_quality_available(mock_deezer_client):
    """Unit test: no fallback when requested quality is available"""
    # Mock track info where FLAC is available
    # quality_map: [(9, "MP3_128"), (3, "MP3_320"), (1, "FLAC")]
    mock_track_info = {
        "FILESIZE_FLAC": 25000000,  # FLAC available (quality 2)
        "FILESIZE_MP3_320": 5000000,  # MP3_320 available (quality 1)
        "FILESIZE_MP3_128": 2000000,  # MP3_128 available (quality 0)
        "TRACK_TOKEN": "test_token",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = "https://test.flac"

    downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

    # Should use requested quality 2 (FLAC)
    assert downloadable.quality == 2


def test_deezer_fallback_to_lowest_available_quality(mock_deezer_client):
    """Unit test: fallback walks down quality list until finding available quality"""
    mock_track_info = {
        "FILESIZE_FLAC": 0,
        "FILESIZE_MP3_320": 0,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.side_effect = _urls_for("MP3_128")

    downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

    # Should have fallen back to quality 0 (MP3_128) since higher qualities unavailable
    assert downloadable.quality == 0
    assert [
        c.args[1] for c in mock_deezer_client.client.get_track_url.call_args_list
    ] == [
        "FLAC",
        "MP3_320",
        "MP3_128",
    ]


def test_deezer_no_url_raises_instead_of_building_legacy_cdn_url(mock_deezer_client):
    """Unit test: a missing download URL fails immediately.

    get_track_url returning None used to fall back to a generated
    e-cdns-proxy-<c>.dzcdn.net URL. Deezer retired that CDN, so the URL was
    guaranteed to fail at download time.
    """
    mock_track_info = {
        "FILESIZE_FLAC": 25000000,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
        "MD5_ORIGIN": "abc123def456abc123def456abc12345",
        "MEDIA_VERSION": "1",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = None

    with pytest.raises(NonStreamableError, match="legacy CDN"):
        arun(mock_deezer_client.get_downloadable("123", quality=2))


def test_deezer_no_url_error_does_not_leak_a_cdn_url(mock_deezer_client):
    """Unit test: no code path may still produce an e-cdns-proxy URL."""
    mock_track_info = {
        "FILESIZE_FLAC": 25000000,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
        "MD5_ORIGIN": "abc123def456abc123def456abc12345",
        "MEDIA_VERSION": "1",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = None

    with pytest.raises(NonStreamableError) as excinfo:
        arun(mock_deezer_client.get_downloadable("123", quality=2))

    assert "e-cdns-proxy" not in str(excinfo.value)


def test_deezer_no_url_follows_fallback_track(mock_deezer_client):
    """Unit test: a delisted track is recovered through FALLBACK.SNG_ID.

    Old-catalog tracks report FILESIZE_* = 0 for every tier and get no URL at
    any quality -- they have been superseded rather than made unavailable, and
    Deezer names the superseding release in FALLBACK. These are exactly the
    tracks that used to reach the retired CDN.
    """

    def gw_get_track(track_id):
        if track_id == "123":
            return {
                "FILESIZE_FLAC": 0,
                "FILESIZE_MP3_320": 0,
                "FILESIZE_MP3_128": 0,
                "TRACK_TOKEN": "token_123",
                "FALLBACK": {"SNG_ID": "456"},
            }
        return {
            "FILESIZE_FLAC": 25000000,
            "FILESIZE_MP3_320": 5000000,
            "FILESIZE_MP3_128": 2000000,
            "TRACK_TOKEN": "token_456",
        }

    mock_deezer_client.client.gw.get_track.side_effect = gw_get_track
    mock_deezer_client.client.get_track_url.side_effect = lambda token, fmt: (
        None if token == "token_123" else "https://test.flac"
    )

    downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

    # The bytes are the fallback track's, so the id -- from which the Blowfish
    # key is derived -- must follow it, not the originally requested track.
    assert downloadable.id == "456"
    # The fallback is asked for at the requested quality, not at the one the
    # zeroed FILESIZEs of the delisted track downgraded to.
    assert downloadable.quality == 2


def test_deezer_fallback_is_not_followed_twice(mock_deezer_client):
    """Unit test: a fallback that itself resolves nowhere raises, not recurses.

    Deezer's FALLBACK chains can point at another dead entry.
    """
    mock_deezer_client.client.gw.get_track.return_value = {
        "FILESIZE_FLAC": 25000000,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
        "FALLBACK": {"SNG_ID": "456"},
    }
    mock_deezer_client.client.get_track_url.return_value = None

    with pytest.raises(NonStreamableError, match="delisted"):
        arun(mock_deezer_client.get_downloadable("123", quality=2))

    # Once for the requested track, once for the fallback - never a third time.
    assert mock_deezer_client.client.gw.get_track.call_count == 2


def test_deezer_no_fallback_when_disabled(mock_deezer_client):
    """Unit test: no fallback when lower_quality_if_not_available is False"""
    mock_deezer_client.config.lower_quality_if_not_available = False

    mock_track_info = {
        "FILESIZE_FLAC": 0,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_url",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.side_effect = _urls_for(
        "MP3_320", "MP3_128"
    )

    # Should raise an error when requested quality is unavailable and fallback is disabled
    with pytest.raises(
        NonStreamableError,
        match="The requested quality 2 is not available and fallback is disabled",
    ):
        arun(mock_deezer_client.get_downloadable("123", quality=2))

    # The lower tiers were never asked for
    assert mock_deezer_client.client.get_track_url.call_count == 1


def test_deezer_listed_size_without_url_does_not_downgrade(mock_deezer_client):
    """Unit test: a size but no URL is a failed request, not a missing format.

    deezer-py turns an HTTP error (a 429, say) into None, the same answer as
    "not served". With a FILESIZE listed for the tier it must not be read as
    a gap: stepping down would silently swap the FLAC for an MP3.
    """
    mock_deezer_client.client.gw.get_track.return_value = {
        "FILESIZE_FLAC": 25000000,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
    }
    mock_deezer_client.client.get_track_url.side_effect = _urls_for(
        "MP3_320", "MP3_128"
    )

    with pytest.raises(NonStreamableError):
        arun(mock_deezer_client.get_downloadable("123", quality=2))

    # The MP3 tiers were never asked for
    assert mock_deezer_client.client.get_track_url.call_count == 1


def test_deezer_wrong_license_steps_down(mock_deezer_client):
    """Unit test: a tier the account may not stream falls back to the next one"""

    def get_track_url(token, fmt):
        if fmt == "FLAC":
            raise deezer.WrongLicense(fmt)
        return "https://cdn/x.mp3"

    mock_deezer_client.client.gw.get_track.return_value = {
        "FILESIZE_FLAC": 25000000,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
    }
    mock_deezer_client.client.get_track_url.side_effect = get_track_url

    downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

    assert downloadable.quality == 1


def test_deezer_album_cache(mock_deezer_client):
    """Unit test: verify get_album results are cached and retrieved on subsequent calls"""
    mock_deezer_client.client.api.get_album.return_value = {
        "id": "album_123",
        "title": "Test Album",
        "genres": {"data": []},
    }
    mock_deezer_client.client.api.get_album_tracks.return_value = {"data": []}

    # Call get_album twice
    res1 = arun(mock_deezer_client.get_album("album_123"))
    res2 = arun(mock_deezer_client.get_album("album_123"))

    # Assert return values are identical
    assert res1 == res2
    assert res1["title"] == "Test Album"

    # Assert api call was made exactly once
    assert mock_deezer_client.client.api.get_album.call_count == 1
    assert mock_deezer_client.client.api.get_album_tracks.call_count == 1


# ===== INTEGRATION TEST =====


@pytest.mark.skipif(
    "DEEZER_ARL" not in os.environ, reason="Deezer ARL not found in env."
)
def test_deezer_fallback_actually_occurred(deezer_client):
    """Integration test: verify fallback works with real track 77874822"""
    # We know track 77874822 doesn't have FLAC available, so test fallback scenario
    downloadable = arun(deezer_client.get_downloadable("77874822", quality=2))

    # Since we requested FLAC (quality=2) but it's not available,
    # we should have fallen back to the next available quality (1 = MP3_320)
    assert downloadable.quality == 1, (
        "Should have fallen back to MP3_320 when FLAC unavailable"
    )
    print("Fallback occurred: FLAC unavailable, fell back to MP3_320")

    # Verify the URL is actually accessible and working
    assert downloadable.url.startswith("https://")
    assert downloadable._size > 0, "Downloadable should have a valid file size"
    assert downloadable.extension == "mp3", "MP3_320 should have .mp3 extension"
