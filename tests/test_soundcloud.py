from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.client.soundcloud import SoundcloudClient, _find_client_id
from streamrip.config import Config

CLIENT_ID = "a" * 16 + "B" * 8 + "1234567z"


def _track(*transcodings, **extra):
    return {
        "id": 1,
        "streamable": True,
        "policy": "ALLOW",
        "downloadable": False,
        "has_downloads_left": False,
        "media": {"transcodings": list(transcodings)},
        **extra,
    }


def _tc(protocol, mime, url):
    return {"url": url, "format": {"protocol": protocol, "mime_type": mime}}


def test_client_id_found_in_bundle():
    assert _find_client_id(f'...,client_id:"{CLIENT_ID}",env:"production"') == CLIENT_ID
    assert _find_client_id(f"fetch('/x?client_id={CLIENT_ID}&a=1')") == CLIENT_ID
    assert _find_client_id("nothing to see here") is None


def test_progressive_mp3_preferred():
    track = _track(
        _tc("hls", "audio/mpeg", "https://x/stream/hls"),
        _tc("progressive", "audio/mpeg", "https://x/stream/progressive"),
    )
    assert SoundcloudClient._get_custom_id(track) == "1|https://x/stream/progressive"


def test_hls_mp3_used_when_no_progressive():
    track = _track(
        _tc("hls", 'audio/mp4; codecs="mp4a.40.2"', "https://x/aac"),
        _tc("hls", "audio/mpeg", "https://x/stream/hls"),
    )
    assert SoundcloudClient._get_custom_id(track) == "1|https://x/stream/hls"


def test_no_mp3_transcoding_is_non_streamable_instead_of_assertion():
    track = _track(_tc("hls", 'audio/ogg; codecs="opus"', "https://x/opus"))
    assert SoundcloudClient._get_custom_id(track).endswith("_non_streamable")


@pytest.mark.asyncio
async def test_refresh_tokens_scans_scripts_from_the_end():
    client = SoundcloudClient(Config.defaults())
    pages = {
        "https://soundcloud.com/": (
            '<script>window.__sc_version="1700000000"</script>'
            '<script src="https://a-v2.sndcdn.com/assets/0-a.js"></script>'
            '<script crossorigin src="https://a-v2.sndcdn.com/assets/49-b.js"></script>'
        ),
        "https://a-v2.sndcdn.com/assets/49-b.js": f'client_id:"{CLIENT_ID}"',
        "https://a-v2.sndcdn.com/assets/0-a.js": "no id here",
    }

    def get(url):
        resp = MagicMock()
        resp.text = AsyncMock(return_value=pages[url])
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    client.session = MagicMock()
    client.session.get = MagicMock(side_effect=get)
    assert await client._refresh_tokens() == (CLIENT_ID, "1700000000")
