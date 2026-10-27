from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.client.downloadable import (
    SegmentedMP4Downloadable,
    SoundcloudDownloadable,
)
from streamrip.client.soundcloud import SoundcloudClient, _find_client_id
from streamrip.config import Config
from streamrip.exceptions import APIError, NonStreamableError
from streamrip.metadata import AlbumMetadata

CLIENT_ID = "a" * 16 + "B" * 8 + "1234567z"
AAC = 'audio/mp4; codecs="mp4a.40.2"'


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


def _tc(protocol, mime, url, **extra):
    return {"url": url, "format": {"protocol": protocol, "mime_type": mime}, **extra}


def test_client_id_found_in_bundle():
    assert _find_client_id(f'...,client_id:"{CLIENT_ID}",env:"production"') == CLIENT_ID
    assert _find_client_id(f"fetch('/x?client_id={CLIENT_ID}&a=1')") == CLIENT_ID
    assert _find_client_id("nothing to see here") is None


@pytest.mark.parametrize(
    ("transcodings", "expected"),
    [
        (
            [
                _tc("hls", "audio/mpeg", "https://x/stream/hls"),
                _tc("progressive", "audio/mpeg", "https://x/stream/progressive"),
            ],
            "1|mp3:https://x/stream/progressive",
        ),
        (
            [
                _tc("hls", "audio/mpeg", "https://x/mp3", quality="sq"),
                _tc("hls", AAC, "https://x/aac", quality="sq"),
            ],
            "1|m4a:https://x/aac",
        ),
        (
            [
                _tc("hls", AAC, "https://x/aac96", quality="lq"),
                _tc("hls", AAC, "https://x/aac160", quality="sq"),
            ],
            "1|m4a:https://x/aac160",
        ),
        (
            [
                _tc("hls", AAC, "https://x/aac96", quality="lq"),
                _tc("hls", "audio/mpeg", "https://x/mp3", quality="sq"),
            ],
            "1|mp3:https://x/mp3",
        ),
        (
            [
                _tc("hls", AAC, "https://x/preview", quality="sq", snipped=True),
                _tc("hls", AAC, "https://x/full", quality="lq"),
            ],
            "1|m4a:https://x/full",
        ),
    ],
    ids=[
        "one file before hls",
        "aac before mp3",
        "higher quality first",
        "quality before codec",
        "no previews",
    ],
)
def test_the_best_stream_is_chosen(transcodings, expected):
    assert SoundcloudClient._get_custom_id(_track(*transcodings)) == expected


def test_a_track_without_a_whole_stream_is_non_streamable():
    """Opus is not one streamrip downloads, and the adaptive stream and DRM
    can't be downloaded whole."""
    track = _track(
        _tc("hls", 'audio/ogg; codecs="opus"', "https://x/opus"),
        _tc("hls", "audio/mpegurl", "https://x/abr"),
        _tc("ctr-encrypted-hls", AAC, "https://x/drm"),
    )
    assert SoundcloudClient._get_custom_id(track).endswith("_non_streamable")


def _playlist_client(playlist: dict, fetched: list[dict]):
    """A client that answers playlist requests from `playlist` and `fetched`."""

    async def request(path, params=None):
        """Answer a request for tracks by id."""
        if path == "tracks":
            ids = params["ids"].split(",")
            return 200, [t for t in fetched if str(t["id"]) in ids]
        return 200, playlist

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

    assert [t["id"] for t in resp["tracks"]] == [
        "1|mp3:https://x/1",
        "2|mp3:https://x/2",
    ]


async def test_playlist_fetches_the_tracks_that_came_without_metadata():
    """Tracks that came without metadata are fetched, once each."""
    playlist = {"tracks": [_mp3(1), {"id": 2}, {"id": 3}, {"id": 2}]}
    client = _playlist_client(playlist, [_mp3(2), _mp3(3)])

    resp = await client._get_playlist("p")

    assert [t["id"] for t in resp["tracks"]] == [
        "1|mp3:https://x/1",
        "2|mp3:https://x/2",
        "3|mp3:https://x/3",
        "2|mp3:https://x/2",
    ]


@pytest.mark.login
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

    async def text(self, _encoding="utf-8"):
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
    out.write_bytes(b"".join(p.read_bytes() for p in paths))


@pytest.fixture
def hls(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    monkeypatch.setattr(
        "streamrip.client.downloadable.concat_audio_files", _fake_concat
    )

    def make(failing=()):
        return SoundcloudDownloadable(_Session(failing), "playlist", "hls")

    return make, temp, tmp_path / "out.mp3"


async def test_hls_segments_are_joined_in_order_and_cleaned_up(hls):
    make, temp, out = hls

    await make()._download(out, lambda _: None)

    assert out.read_bytes() == b"012"
    assert list(temp.iterdir()) == []


async def test_hls_segments_are_cleaned_up_when_one_fails(hls):
    make, temp, out = hls

    with pytest.raises(ConnectionError):
        await make(failing={"https://seg/1.mp3"})._download(out, lambda _: None)

    assert list(temp.iterdir()) == []


def _client_answering(responses):
    """A client whose API calls answer from {url suffix: (status, json)}."""
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


async def test_refused_original_falls_back_to_the_mp3_stream():
    """Anonymously, tracks/<id>/download answers 401 even for downloadable
    tracks (soundcloud.com/forss/flickermood, the API docs' own example), which
    failed them outright. Their MP3 stream is used instead."""
    stream = _tc("progressive", "audio/mpeg", "https://x/stream/progressive")
    client = _client_answering(
        {
            "tracks/293/download": (401, {}),
            "tracks/293": (200, _track(stream)),
            "/stream/progressive": (200, {"url": "https://cdn/signed.mp3"}),
        }
    )

    d = await client.get_downloadable("293|_original_download", 0)

    assert d.url == "https://cdn/signed.mp3"
    assert d.extension == "mp3"


async def test_refused_original_without_a_stream_is_non_streamable():
    client = _client_answering(
        {"tracks/293/download": (401, {}), "tracks/293": (200, _track())}
    )
    with pytest.raises(NonStreamableError, match="no stream"):
        await client.get_downloadable("293|_original_download", 0)


async def test_aac_over_hls_downloads_its_init_and_media_segments():
    """Most tracks only have AAC now: fragmented MP4 over HLS, an init segment
    (EXT-X-MAP) the media segments only play after."""
    client = _client_answering(
        {"/stream/hls": (200, {"url": "https://cdn/a/playlist.m3u8"})}
    )
    playlist = (
        '#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-MAP:URI="init.mp4"\n'
        "#EXTINF:10.0,\ndata000.m4s\n#EXTINF:10.0,\ndata001.m4s\n#EXT-X-ENDLIST\n"
    )
    client.session.get = MagicMock(return_value=_Resp(playlist.encode()))

    d = await client.get_downloadable("7|m4a:https://x/stream/hls", 0)

    assert isinstance(d, SegmentedMP4Downloadable)
    assert (d.source, d.extension) == ("soundcloud", "m4a")
    assert d.init_url == "https://cdn/a/init.mp4"
    assert d.segment_urls == [
        "https://cdn/a/data000.m4s",
        "https://cdn/a/data001.m4s",
    ]


@pytest.mark.parametrize(("mime", "container"), [(AAC, "AAC"), ("audio/mpeg", "MP3")])
async def test_the_container_is_the_one_the_download_makes(mime, container):
    """It names folders ("[{container}]") and labels the progress bar."""
    track = _track(
        _tc("hls", mime, "https://x/s", quality="sq"),
        id=7,
        user={"username": "u"},
        artwork_url="https://i/large.jpg",
    )
    client = _client_answering({"tracks/7": (200, track)})

    resp = await client.get_metadata("7|whatever", "track")

    assert AlbumMetadata.from_soundcloud(resp).info.container == container


async def test_mp3_over_hls_is_still_joined_from_its_segments():
    client = _client_answering(
        {"/stream/hls": (200, {"url": "https://cdn/a/playlist.m3u8"})}
    )
    d = await client.get_downloadable("7|mp3:https://x/stream/hls", 0)
    assert (d.file_type, d.extension) == ("hls", "mp3")


async def test_an_error_answer_without_json_reports_its_status():
    """That 401 has an empty body; resp.json() used to raise ContentTypeError
    ("Attempt to decode JSON with unexpected mimetype") before anyone read the
    status."""
    client = SoundcloudClient(Config.defaults())
    resp = MagicMock(status=401, content_type="text/html")
    resp.json = AsyncMock(return_value=None)  # aiohttp's answer for no body

    async def get_with_retries(url, read, params, headers):
        return await read(resp)

    client._get_with_retries = get_with_retries

    status, body = await client._request("https://api-v2.soundcloud.com/tracks/1")
    assert (status, body) == (401, {"message": "non-JSON response (text/html)"})


async def test_a_failed_request_says_which_and_why():
    """These were bare `assert status == 200`s: an AssertionError, or nothing
    at all under python -O."""
    client = _client_answering({"tracks/7": (404, {})})
    with pytest.raises(APIError, match=r"tracks/7 failed \(HTTP 404\)"):
        await client.get_metadata("7|whatever", "track")
