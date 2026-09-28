import base64
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.client.downloadable import TidalDASHDownloadable, TidalDownloadable
from streamrip.client.tidal import (
    DEFAULT_CLIENT_ID,
    HIRES_CLIENT_ID,
    TidalClient,
)
from streamrip.config import Config
from streamrip.exceptions import MissingCredentialsError, NonStreamableError
from streamrip.metadata.util import tidal_quality_id

MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:isoff-main:2011">
  <Period id="0">
    <AdaptationSet id="0" contentType="audio" mimeType="audio/mp4">
      <Representation id="FLAC,96000,24" codecs="flac" bandwidth="3000000"
                      audioSamplingRate="96000">
        <SegmentTemplate timescale="96000" initialization="https://sp.example/init.mp4"
                         media="https://sp.example/$Number$.mp4" startNumber="1">
          <SegmentTimeline>
            <S d="384000" r="2"/>
            <S d="100000"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def _client() -> TidalClient:
    return TidalClient(Config.defaults())


def test_default_client_is_the_lossless_one():
    assert _client().client_id == DEFAULT_CLIENT_ID


def test_default_quality_is_lossless_not_hires():
    # Regression guard: most catalogs mostly don't have a hi-res master, so
    # defaulting to 3 (HI_RES) mainly just produces a warning-per-track for
    # no benefit -- see the commit that changed this. A future template
    # edit reverting to 3 should fail this test, not surface as log noise.
    assert Config.defaults().session.tidal.quality == 2


def test_hires_client_can_be_selected():
    cfg = Config.defaults()
    cfg.session.tidal.hires_client = True
    assert TidalClient(cfg).client_id == HIRES_CLIENT_ID


def test_explicit_client_override_wins():
    cfg = Config.defaults()
    cfg.session.tidal.hires_client = True
    cfg.session.tidal.client_id = "abc"
    cfg.session.tidal.client_secret = "xyz"
    assert TidalClient(cfg).client_id == "abc"


@pytest.mark.asyncio
async def test_token_from_other_client_requires_new_login(monkeypatch):
    c = _client()
    c.config.access_token = "token"
    c.config.token_client_id = HIRES_CLIENT_ID
    monkeypatch.setattr("streamrip.client.tidal.new_session", lambda **_: MagicMock())
    with pytest.raises(MissingCredentialsError):
        await c.login()


@pytest.mark.asyncio
async def test_dash_manifest_becomes_segmented_downloadable():
    c = _client()
    c.session = MagicMock()
    c._api_request = AsyncMock(
        return_value={
            "manifestMimeType": "application/dash+xml",
            "manifest": base64.b64encode(MPD.encode()).decode(),
        }
    )
    dl = await c.get_downloadable("1", 3)
    assert isinstance(dl, TidalDASHDownloadable)
    assert dl.extension == "flac"
    assert dl.init_url == "https://sp.example/init.mp4"
    assert dl.segment_urls == [f"https://sp.example/{n}.mp4" for n in range(1, 5)]


@pytest.mark.asyncio
async def test_json_manifest_still_supported():
    c = _client()
    c.session = MagicMock()
    manifest = (
        '{"urls": ["https://x/y.flac"], "codecs": "flac", "encryptionType": "NONE"}'
    )
    c._api_request = AsyncMock(
        return_value={
            "manifestMimeType": "application/vnd.tidal.bts",
            "manifest": base64.b64encode(manifest.encode()).decode(),
        }
    )
    dl = await c.get_downloadable("1", 2)
    assert isinstance(dl, TidalDownloadable)
    assert dl.extension == "flac"


@pytest.mark.asyncio
async def test_missing_manifest_is_non_streamable():
    c = _client()
    c._api_request = AsyncMock(return_value={"userMessage": "Asset is not ready"})
    with pytest.raises(NonStreamableError, match="not ready"):
        await c.get_downloadable("1", 2)


@pytest.mark.asyncio
async def test_warns_when_lossless_is_requested_but_lossy_is_served(caplog):
    c = _client()
    c.session = MagicMock()
    manifest = (
        '{"urls": ["https://x/y.m4a"], "codecs": "mp4a.40.2", "encryptionType": "NONE"}'
    )
    c._api_request = AsyncMock(
        return_value={
            "audioQuality": "HIGH",
            "manifestMimeType": "application/vnd.tidal.bts",
            "manifest": base64.b64encode(manifest.encode()).decode(),
        }
    )
    await c.get_downloadable("1", 2)  # requested LOSSLESS, only AAC available
    assert "requested LOSSLESS but Tidal only has HIGH" in caplog.text


@pytest.mark.asyncio
async def test_hires_request_served_lossless_is_not_a_warning(caplog):
    """Quality 3 is "best available", so most tracks legitimately get LOSSLESS."""
    c = _client()
    c.session = MagicMock()
    manifest = (
        '{"urls": ["https://x/y.flac"], "codecs": "flac", "encryptionType": "NONE"}'
    )
    c._api_request = AsyncMock(
        return_value={
            "audioQuality": "LOSSLESS",
            "manifestMimeType": "application/vnd.tidal.bts",
            "manifest": base64.b64encode(manifest.encode()).decode(),
        }
    )
    with caplog.at_level(logging.DEBUG, logger="streamrip"):
        await c.get_downloadable("1", 3)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert "no hi-res master" in caplog.text


@pytest.mark.asyncio
async def test_no_warning_when_served_quality_matches_or_exceeds_request(caplog):
    c = _client()
    c.session = MagicMock()
    manifest = (
        '{"urls": ["https://x/y.flac"], "codecs": "flac", "encryptionType": "NONE"}'
    )
    c._api_request = AsyncMock(
        return_value={
            "audioQuality": "LOSSLESS",
            "manifestMimeType": "application/vnd.tidal.bts",
            "manifest": base64.b64encode(manifest.encode()).decode(),
        }
    )
    # Should not raise or warn: got exactly what was requested.
    dl = await c.get_downloadable("1", 2)
    assert "requested" not in caplog.text
    assert isinstance(dl, TidalDownloadable)


@pytest.mark.asyncio
async def test_single_search_hit_is_returned():
    c = _client()
    c._api_request = AsyncMock(return_value={"items": [{"id": 1}]})
    assert await c.search("track", "q", limit=1) == [{"items": [{"id": 1}]}]


def test_unknown_tidal_quality_does_not_crash():
    assert tidal_quality_id("HI_RES_LOSSLESS") == 3
    assert tidal_quality_id("SOMETHING_NEW") == 2
    assert tidal_quality_id(None) == 0


@pytest.mark.asyncio
async def test_album_items_are_paged_and_videos_left_out():
    total = 250

    async def api(path, params=None):
        if path == "albums/1":
            return {"id": 1, "numberOfTracks": total - 1, "numberOfVideos": 1}
        offset = (params or {}).get("offset", 0)
        return {
            "totalNumberOfItems": total,
            "items": [
                {"type": "video" if n == 5 else "track", "item": {"id": n}}
                for n in range(offset, min(offset + 100, total))
            ],
        }

    c = _client()
    c._api_request = AsyncMock(side_effect=api)

    album = await c.get_metadata("1", "album")

    assert [t["id"] for t in album["tracks"]] == [n for n in range(total) if n != 5]
    # The album, then one request per page of 100 -- no empty page past the end.
    assert c._api_request.await_count == 1 + 3
