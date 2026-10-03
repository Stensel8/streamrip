from unittest.mock import AsyncMock, MagicMock

import aiohttp
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


def _playlist_client(playlist: dict, fetched: list[dict]):
    """A client that answers playlist requests from `playlist` and `fetched`."""

    async def request(path, params=None):
        """Answer a request for tracks by id."""
        if path == "tracks":
            ids = params["ids"].split(",")
            return [t for t in fetched if str(t["id"]) in ids], 200
        return playlist, 200

    client = SoundcloudClient(Config.defaults())
    client._api_request = AsyncMock(side_effect=request)
    return client


def _mp3(n):
    """A track with one progressive MP3 transcoding."""
    return _track(_tc("progressive", "audio/mpeg", f"https://x/{n}"), id=n)


async def test_playlist_that_comes_complete_still_gets_custom_ids():
    """A playlist that comes complete still gets custom ids.

    Small playlists come with every track's metadata, so there is nothing to fetch.
    Skipping the ids too used to make every track fail.
    """
    client = _playlist_client({"tracks": [_mp3(1), _mp3(2)]}, [])

    resp = await client._get_playlist("p")

    assert [t["id"] for t in resp["tracks"]] == ["1|https://x/1", "2|https://x/2"]


async def test_playlist_fetches_the_tracks_that_came_without_metadata():
    """Tracks that came without metadata are fetched, once each."""
    playlist = {"tracks": [_mp3(1), {"id": 2}, {"id": 3}, {"id": 2}]}
    client = _playlist_client(playlist, [_mp3(2), _mp3(3)])

    resp = await client._get_playlist("p")

    assert [t["id"] for t in resp["tracks"]] == [
        "1|https://x/1",
        "2|https://x/2",
        "3|https://x/3",
        "2|https://x/2",
    ]


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

    def get(url, **_):
        resp = MagicMock(status=200)
        resp.text = AsyncMock(return_value=pages[url])
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    client.session = MagicMock()
    client.session.get = MagicMock(side_effect=get)
    assert await client._refresh_tokens() == (CLIENT_ID, "1700000000")


M3U = (
    "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
    + "".join(f"#EXTINF:10.0,\nhttps://seg/{n}.mp3\n" for n in range(3))
    + "#EXT-X-ENDLIST\n"
)


class _Resp:
    def __init__(self, body: bytes = b"", fail: bool = False):
        self.body, self.fail = body, fail
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def raise_for_status(self):
        if self.fail:
            raise ConnectionError("segment failed")

    async def text(self, _encoding):
        return self.body.decode()

    async def read(self):
        return self.body


class _Session:
    def __init__(self, failing=()):
        self.failing = failing

    def get(self, url):
        if url == "playlist":
            return _Resp(M3U.encode())
        n = url.rsplit("/", 1)[1].split(".")[0]
        return _Resp(n.encode(), fail=url in self.failing)


async def _fake_concat(paths, out, _ext):
    with open(out, "wb") as f:
        for p in paths:
            with open(p, "rb") as segment:
                f.write(segment.read())


@pytest.fixture
def hls(tmp_path, monkeypatch):
    from streamrip.client.downloadable import SoundcloudDownloadable

    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    monkeypatch.setattr(
        "streamrip.client.downloadable.concat_audio_files", _fake_concat
    )

    def make(failing=()):
        info = {"type": "mp3", "url": "playlist"}
        return SoundcloudDownloadable(_Session(failing), info)

    return make, temp, tmp_path / "out.mp3"


async def test_hls_segments_are_joined_in_order_and_cleaned_up(hls):
    make, temp, out = hls

    await make()._download(str(out), lambda _: None)

    assert out.read_bytes() == b"012"
    assert list(temp.iterdir()) == []


async def test_hls_segments_are_cleaned_up_when_one_fails(hls):
    make, temp, out = hls

    with pytest.raises(ConnectionError):
        await make(failing={"https://seg/1.mp3"})._download(str(out), lambda _: None)

    assert list(temp.iterdir()) == []


def _client_answering(responses):
    """A client whose API calls answer from {url suffix: (json, status)}."""
    client = SoundcloudClient(Config.defaults())
    client.session = MagicMock()

    async def answer(path_or_url, params=None, headers=None):
        for suffix, value in responses.items():
            if path_or_url.endswith(suffix):
                return value
        raise AssertionError(f"unexpected request {path_or_url}")

    client._api_request = AsyncMock(side_effect=answer)
    client._request = AsyncMock(side_effect=answer)
    return client


@pytest.mark.asyncio
async def test_refused_original_falls_back_to_the_mp3_stream():
    """Anonymously, tracks/<id>/download answers 401 even for downloadable
    tracks (soundcloud.com/forss/flickermood, the API docs' own example), which
    failed them outright. Their MP3 stream is used instead."""
    client = _client_answering(
        {
            "tracks/293/download": ({}, 401),
            "tracks/293": (
                _track(
                    _tc("progressive", "audio/mpeg", "https://x/stream/progressive")
                ),
                200,
            ),
            "/stream/progressive": ({"url": "https://cdn/signed.mp3"}, 200),
        }
    )

    d = await client.get_downloadable("293|_original_download", None)

    assert d.url == "https://cdn/signed.mp3"
    assert d.extension == "mp3"


@pytest.mark.asyncio
async def test_refused_original_without_a_stream_is_non_streamable():
    from streamrip.exceptions import NonStreamableError

    client = _client_answering(
        {"tracks/293/download": ({}, 401), "tracks/293": (_track(), 200)}
    )
    with pytest.raises(NonStreamableError, match="no MP3 stream"):
        await client.get_downloadable("293|_original_download", None)


@pytest.mark.asyncio
async def test_an_error_answer_that_is_not_json_reports_its_status():
    """That 401 has an empty body; resp.json() raised ContentTypeError ("Attempt
    to decode JSON with unexpected mimetype") before anyone read the status."""
    client = SoundcloudClient(Config.defaults())
    resp = MagicMock(status=401)
    resp.json = AsyncMock(
        side_effect=aiohttp.ContentTypeError(
            MagicMock(), (), message="Attempt to decode JSON with unexpected mimetype"
        )
    )

    async def get_with_retries(url, read, params, headers):
        return await read(resp)

    client._get_with_retries = get_with_retries

    assert await client._request("https://api-v2.soundcloud.com/tracks/1/download") == (
        {},
        401,
    )
