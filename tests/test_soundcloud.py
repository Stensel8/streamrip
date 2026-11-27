from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.client.downloadable import (
    SegmentedMP4Downloadable,
    SoundcloudDownloadable,
)
from streamrip.client.soundcloud import SoundcloudClient
from streamrip.config import Config
from streamrip.exceptions import APIError, NonStreamableError, SearchTypeError

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


def _mp3(n):
    return _track(_tc("progressive", "audio/mpeg", f"https://x/{n}"), id=n)


def _client(responses=None):
    """A client whose API calls answer from {url suffix: (status, json)}."""
    client = SoundcloudClient(Config.defaults())
    client.session = MagicMock()

    async def answer(path_or_url, params=None, headers=None):
        for suffix, value in (responses or {}).items():
            if path_or_url.endswith(suffix):
                return value
        raise AssertionError(f"unexpected request {path_or_url}")

    client._api_request = AsyncMock(side_effect=answer)
    client._request = AsyncMock(side_effect=answer)
    return client


class _Resp:
    status = 200

    def __init__(self, body: str, fail: bool = False):
        self.body, self.fail = body, fail
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def raise_for_status(self):
        if self.fail:
            raise ConnectionError("segment failed")

    async def text(self, *_, **__):
        return self.body

    async def read(self):
        return self.body.encode()


class _Session:
    """Answers GETs from {url: body}; the urls in `failing` fail."""

    def __init__(self, pages, failing=()):
        self.pages, self.failing = pages, failing

    def get(self, url, **_):
        return _Resp(self.pages[url], fail=url in self.failing)


@pytest.mark.login
async def test_client_id_is_found_in_the_newest_script():
    client = SoundcloudClient(Config.defaults())
    client.rate_limiter = nullcontext()
    client.session = _Session(
        {
            "https://soundcloud.com/": (
                '<script>window.__sc_version="1700000000"</script>'
                '<script src="https://a-v2.sndcdn.com/assets/0-a.js"></script>'
                '<script crossorigin src="https://a-v2.sndcdn.com/assets/49-b.js">'
                "</script>"
            ),
            "https://a-v2.sndcdn.com/assets/49-b.js": f'client_id:"{CLIENT_ID}"',
            "https://a-v2.sndcdn.com/assets/0-a.js": "no id here",
        }
    )
    assert await client._refresh_tokens() == (CLIENT_ID, "1700000000")


@pytest.mark.parametrize(
    ("transcodings", "expected"),
    [
        (
            [
                _tc("hls", "audio/mpeg", "https://x/hls"),
                _tc("progressive", "audio/mpeg", "https://x/progressive"),
            ],
            "1|mp3:https://x/progressive",
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
                _tc("hls", AAC, "https://x/preview", quality="sq", snipped=True),
                _tc("hls", AAC, "https://x/full", quality="lq"),
            ],
            "1|m4a:https://x/full",
        ),
        (
            [
                _tc("hls", 'audio/ogg; codecs="opus"', "https://x/opus"),
                _tc("ctr-encrypted-hls", AAC, "https://x/drm"),
            ],
            "1|_drm_only",
        ),
        (
            [_tc("hls", 'audio/ogg; codecs="opus"', "https://x/opus")],
            "1|_non_streamable",
        ),
    ],
    ids=["progressive first", "aac over mp3", "no previews", "drm only", "nothing"],
)
def test_the_best_stream_is_chosen(transcodings, expected):
    assert SoundcloudClient._get_custom_id(_track(*transcodings)) == expected


async def test_search_tracks_but_not_albums():
    client = _client({"search/tracks": (200, {"collection": [_mp3(5)]})})
    [page] = await client.search("track", "q")
    assert page["collection"][0]["id"] == "5|mp3:https://x/5"
    with pytest.raises(SearchTypeError):
        await client.search("album", "q")


@pytest.mark.parametrize(
    ("listed", "fetched", "ids"),
    [
        # Small playlists list every track in full. Skipping their custom ids
        # once made every track fail.
        ([_mp3(1), _mp3(2)], [], [1, 2]),
        ([_mp3(1), {"id": 2}, {"id": 3}], [_mp3(2), _mp3(3)], [1, 2, 3]),
    ],
    ids=["complete", "tracks without metadata"],
)
async def test_a_playlist_gets_custom_ids(listed, fetched, ids):
    client = _client(
        {"playlists/p": (200, {"tracks": listed}), "tracks": (200, fetched)}
    )
    resp = await client._get_playlist("p")
    assert [t["id"] for t in resp["tracks"]] == [f"{n}|mp3:https://x/{n}" for n in ids]


@pytest.mark.parametrize("has_stream", [True, False])
async def test_a_refused_original_falls_back_to_the_stream(has_stream):
    # Anonymously, tracks/<id>/download answers 401 even for downloadable tracks.
    stream = _tc("progressive", "audio/mpeg", "https://x/stream/progressive")
    client = _client(
        {
            "tracks/293/download": (401, {}),
            "tracks/293": (200, _track(stream) if has_stream else _track()),
            "/stream/progressive": (200, {"url": "https://cdn/signed.mp3"}),
        }
    )
    if has_stream:
        d = await client.get_downloadable("293|_original_download", 0)
        assert (d.url, d.extension) == ("https://cdn/signed.mp3", "mp3")
    else:
        with pytest.raises(NonStreamableError):
            await client.get_downloadable("293|_original_download", 0)


async def test_aac_over_hls_has_an_init_and_media_segments():
    client = _client({"/stream/hls": (200, {"url": "https://cdn/a/playlist.m3u8"})})
    client.session = _Session(
        {
            "https://cdn/a/playlist.m3u8": (
                '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
                "#EXTINF:10.0,\ndata000.m4s\n#EXTINF:10.0,\ndata001.m4s\n"
                "#EXT-X-ENDLIST\n"
            )
        }
    )
    d = await client.get_downloadable("7|m4a:https://x/stream/hls", 0)
    assert isinstance(d, SegmentedMP4Downloadable)
    assert d.init_url == "https://cdn/a/init.mp4"
    assert d.segment_urls == ["https://cdn/a/data000.m4s", "https://cdn/a/data001.m4s"]


@pytest.mark.parametrize("item_id", ["7|mp3:https://x/stream/hls", "7"])
async def test_mp3_over_hls_is_joined_from_segments(item_id):
    # A failed track is recorded by its bare id, which is looked up again.
    track = _track(_tc("hls", "audio/mpeg", "https://x/stream/hls"), id=7)
    client = _client(
        {
            "tracks/7": (200, {**track, "user": {"username": "u"}}),
            "/stream/hls": (200, {"url": "https://cdn/a/playlist.m3u8"}),
        }
    )
    d = await client.get_downloadable(item_id, 0)
    assert (d.file_type, d.extension) == ("hls", "mp3")


@pytest.mark.parametrize(
    "failing", [(), {"https://seg/1.mp3"}], ids=["all there", "a segment fails"]
)
async def test_hls_segments_are_joined_in_order_and_cleaned_up(
    tmp_path, monkeypatch, failing
):
    async def concat(paths, out, _ext):
        out.write_bytes(b"".join(p.read_bytes() for p in paths))

    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    monkeypatch.setattr("streamrip.client.downloadable.concat_audio_files", concat)
    m3u = "#EXTM3U\n" + "".join(
        f"#EXTINF:10.0,\nhttps://seg/{n}.mp3\n" for n in range(3)
    )
    pages = {"playlist": m3u} | {f"https://seg/{n}.mp3": str(n) for n in range(3)}
    out = tmp_path / "out.mp3"
    downloadable = SoundcloudDownloadable(_Session(pages, failing), "playlist", "hls")

    with pytest.raises(ConnectionError) if failing else nullcontext():
        await downloadable._download(out, lambda _: None)

    if failing:
        assert not out.exists()
    else:
        assert out.read_bytes() == b"012"
    assert list(temp.iterdir()) == []


async def test_a_failed_request_raises_an_api_error():
    client = _client({"tracks/7": (404, {})})
    with pytest.raises(APIError, match="404"):
        await client.get_metadata("7|whatever", "track")


@pytest.mark.login
async def test_a_login_is_sent_and_a_refused_one_means_guest():
    config = Config.defaults()
    config.session.soundcloud.oauth_token = "2-1234"
    client = SoundcloudClient(config)
    go_plus = {"consumer_subscription": {"product": {"id": "consumer-high-tier"}}}
    client._get_json = AsyncMock(return_value=(200, {"username": "sten", **go_plus}))
    await client._sign_in()
    assert client._get_json.call_args.args[2] == {"Authorization": "OAuth 2-1234"}
    assert await client.account_plan() == ("sten, Go+", 2)

    client._get_json = AsyncMock(return_value=(401, {"message": "no"}))
    await client._sign_in()
    assert "HTTP 401" in client.guest_reason
    assert client._auth_headers() is None
    assert config.session.soundcloud.oauth_token == "2-1234"  # the user's to remove
    assert await client.account_plan() == ("guest", None)
