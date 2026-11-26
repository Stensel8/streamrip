import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from streamrip.client.downloadable import (
    BasicDownloadable,
    SegmentedMP4Downloadable,
    TidalDownloadable,
)
from streamrip.client.tidal import DEFAULT_CLIENT_ID, HIRES_CLIENT_ID, TidalClient
from streamrip.config import Config
from streamrip.exceptions import (
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
    SearchTypeError,
)
from streamrip.metadata import AlbumMetadata, ArtistMetadata, TrackMetadata
from streamrip.rip.prompter import TidalPrompter

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
HIRES = ["LOSSLESS", "HIRES_LOSSLESS"]
FORMAT = {"bitDepth": 24, "sampleRate": 96000}


def _client(hires: bool = False) -> TidalClient:
    """One lane (the lossless client) unless `hires`."""
    cfg = Config.defaults()
    cfg.session.tidal.hires_client = hires
    return TidalClient(cfg)


def _dash() -> dict:
    return {
        "audioQuality": "HI_RES_LOSSLESS",
        "bitDepth": 24,
        "sampleRate": 96000,
        "manifestMimeType": "application/dash+xml",
        "manifest": base64.b64encode(MPD.encode()).decode(),
    }


def _bts(quality: str, codec: str = "flac") -> dict:
    manifest = (
        f'{{"urls": ["https://x/y"], "codecs": "{codec}", "encryptionType": "NONE"}}'
    )
    return {
        "audioQuality": quality,
        "manifestMimeType": "application/vnd.tidal.bts",
        "manifest": base64.b64encode(manifest.encode()).decode(),
    }


@pytest.mark.parametrize(
    ("settings", "lanes"),
    [
        ({}, [DEFAULT_CLIENT_ID, HIRES_CLIENT_ID]),
        ({"quality": 2}, [DEFAULT_CLIENT_ID]),  # streamrip --quality 2
    ],
)
def test_lanes(settings, lanes):
    cfg = Config.defaults()
    for name, value in settings.items():
        setattr(cfg.session.tidal, name, value)
    assert [lane.client_id for lane in TidalClient(cfg).lanes()] == lanes


@pytest.mark.login
async def test_a_token_from_the_other_client_requires_a_new_login(monkeypatch):
    c = _client()
    c.config.access_token = "token"
    c.config.token_client_id = HIRES_CLIENT_ID
    monkeypatch.setattr("streamrip.client.client.new_session", lambda **_: MagicMock())
    with pytest.raises(MissingCredentialsError):
        await c.login()


async def test_the_login_goes_with_api_requests_only(serve, tmp_path):
    # The same session fetches the files, from whatever address a manifest names.
    seen: dict[str, set[str]] = {}

    async def answer(request):
        seen[f"{request.method} {request.path}"] = {
            name.lower() for name in request.headers
        }
        if request.path == "/v1/me":
            return web.json_response({})
        return web.Response(body=b"audio")

    base = await serve({"/v1/me": answer, "/file": answer})
    c = _client()
    c._ensure_session()
    try:
        c.tokens.access_token = "ACCESS"
        c._update_authorization_from_config()
        await c._api_request("me", base=f"{base}v1")
        file = BasicDownloadable(c.session, f"{base}file", "flac")
        await file.size()
        await file.download(tmp_path / "track.flac", lambda _: None)
    finally:
        await c.close()

    assert "authorization" in seen["GET /v1/me"]
    assert "authorization" not in seen["HEAD /file"] | seen["GET /file"]


def _mock_login(lane: TidalClient, expires_in=300, status=0) -> None:
    """Make a lane log in without a network."""
    lane.session = MagicMock()
    lane._get_device_code = AsyncMock(
        return_value=("code", "link.tidal.com/X", expires_in)
    )
    info = {
        "user_id": 1,
        "country_code": "NL",
        "access_token": f"at-{lane.client_id}",
        "refresh_token": f"rt-{lane.client_id}",
        "token_expiry": 123.0,
        "error": "access_denied",
    }
    lane._get_auth_status = AsyncMock(return_value=(status, info))


@pytest.fixture
def quiet_prompter(monkeypatch):
    monkeypatch.setattr("streamrip.rip.prompter.launch", lambda *_: None)
    monkeypatch.setattr("streamrip.rip.prompter.console", MagicMock())


@pytest.mark.login
async def test_the_prompter_logs_both_lanes_in_and_saves_them(quiet_prompter):
    cfg = Config.defaults()
    client = TidalClient(cfg)
    for lane in client.lanes():
        _mock_login(lane)
    prompter = TidalPrompter(cfg, client)
    assert not prompter.has_creds()

    await prompter.prompt_and_login()
    prompter.save()

    saved = cfg.file.tidal
    assert prompter.has_creds()
    assert saved.access_token == f"at-{DEFAULT_CLIENT_ID}"
    assert saved.hires_access_token == f"at-{HIRES_CLIENT_ID}"
    assert saved.user_id == 1


@pytest.mark.login
@pytest.mark.parametrize(
    ("expires_in", "status", "error"),
    [(0, 0, "expired"), (300, 1, "access_denied")],
    ids=["the link expired", "Tidal said no"],
)
async def test_a_device_login_that_fails_says_why(
    quiet_prompter, expires_in, status, error
):
    cfg = Config.defaults()
    client = TidalClient(cfg)
    _mock_login(client, expires_in, status)
    with pytest.raises(AuthenticationError, match=error):
        await TidalPrompter(cfg, client)._device_login(client)


@pytest.mark.login
@pytest.mark.parametrize(
    ("resp", "message"),
    [
        (
            {
                "status": 403,
                "error": "abuse_detected",
                "error_description": "User account is suspended due to abuse",
            },
            "suspended due to abuse",
        ),
        ({"status": 400}, "expired or been revoked"),
    ],
)
async def test_a_refused_token_refresh_says_why(resp, message):
    client = TidalClient(Config.defaults())
    client._api_post = AsyncMock(return_value=resp)
    with pytest.raises(AuthenticationError, match=message):
        await client._refresh_access_token()


async def test_a_dash_manifest_becomes_a_segmented_downloadable():
    c = _client()
    c.session = MagicMock()
    c._api_request = AsyncMock(return_value=_dash())
    dl = await c.get_downloadable("1", 3)
    assert isinstance(dl, SegmentedMP4Downloadable)
    assert dl.extension == "flac"
    assert dl.init_url == "https://sp.example/init.mp4"
    assert dl.segment_urls == [f"https://sp.example/{n}.mp4" for n in range(1, 5)]
    # 3 Mb/s for (3 x 384000 + 100000) / 96000 seconds
    assert await dl.size() == 4_890_625


@pytest.mark.parametrize(
    ("served", "codec", "extension"),
    [("LOSSLESS", "flac", "flac"), ("HIGH", "mp4a.40.2", "m4a")],
)
async def test_a_stream_gets_its_extension_from_its_codec(served, codec, extension):
    c = _client()
    c.session = MagicMock()
    c._api_request = AsyncMock(return_value=_bts(served, codec))
    dl = await c.get_downloadable("1", 2)
    assert isinstance(dl, TidalDownloadable)
    assert dl.extension == extension


NOT_ASKED = AssertionError("the hi-res client must not be asked")


@pytest.mark.parametrize(
    ("asked", "tags", "hires_reply", "kind"),
    [
        (3, None, _dash(), SegmentedMP4Downloadable),
        (3, None, _bts("HIGH", "mp4a.40.2"), TidalDownloadable),  # no hi-res master
        (3, None, RuntimeError("boom"), TidalDownloadable),
        (2, None, NOT_ASKED, TidalDownloadable),  # CD quality
        (3, ["LOSSLESS"], NOT_ASKED, TidalDownloadable),  # tagged: no hi-res master
    ],
    ids=["hi-res", "no master", "hi-res fails", "cd", "tagged cd"],
)
async def test_which_lane_serves_a_track(asked, tags, hires_reply, kind):
    c = _client(hires=True)
    c.session = c.hires_lane.session = MagicMock()
    lossless = NOT_ASKED if kind is SegmentedMP4Downloadable else _bts("LOSSLESS")
    for lane, reply in ((c.hires_lane, hires_reply), (c, lossless)):
        key = "side_effect" if isinstance(reply, Exception) else "return_value"
        lane._api_request = AsyncMock(**{key: reply})
    if tags is not None:
        c._note_hires_tags([{"id": 1, "mediaMetadata": {"tags": tags}}])
    assert isinstance(await c.get_downloadable("1", asked), kind)


async def test_nothing_streamable_raises_tidals_message():
    c = _client(hires=True)
    for lane in c.lanes():
        lane._api_request = AsyncMock(
            return_value={"userMessage": "Asset is not ready"}
        )
    with pytest.raises(NonStreamableError, match="not ready"):
        await c.get_downloadable("1", 3)


def _album_replies(tags: list[str]):
    """An album with two tracks, as `_api_request` would answer for it."""
    album = {"id": 1, "mediaMetadata": {"tags": tags}}
    items = {
        "totalNumberOfItems": 2,
        "items": [
            {"type": "track", "item": {"id": i, "album": {"id": 1}}} for i in (11, 12)
        ],
    }

    async def reply(path, *_, **__):
        return items if "/items" in path else dict(album)

    return reply


@pytest.mark.parametrize(
    ("tags", "lookup", "expected"),
    [
        (HIRES, _dash(), FORMAT),
        (["LOSSLESS"], NOT_ASKED, None),
        (HIRES, RuntimeError("429"), None),  # only the default label
    ],
)
async def test_an_albums_real_format(tags, lookup, expected):
    c = _client(hires=True)
    c._api_request = _album_replies(tags)
    key = "side_effect" if isinstance(lookup, Exception) else "return_value"
    c.hires_lane._api_request = AsyncMock(**{key: lookup})
    album = await c.get_metadata("1", "album")
    assert album.get("streamQuality") == expected


async def test_a_hires_single_reports_its_real_format():
    c = _client(hires=True)
    c.global_config.session.filepaths.add_singles_to_folder = True
    tagged = {"mediaMetadata": {"tags": ["HIRES_LOSSLESS"]}}

    async def reply(path, *_, **__):
        if path.startswith("tracks/"):
            return {"id": 11, "album": {"id": 1}, **tagged}
        return {"id": 1, **tagged}

    c._api_request = reply
    c.hires_lane._api_request = AsyncMock(return_value=_dash())
    track = await c.get_metadata("11", "track")
    assert track["album"]["streamQuality"] == FORMAT


async def test_search():
    c = _client()
    c._api_request = AsyncMock(return_value={"items": [{"id": 1}]})
    assert await c.search("track", "q", limit=1) == [{"items": [{"id": 1}]}]
    c._api_request.return_value = {"items": []}
    assert await c.search("track", "q") == []
    with pytest.raises(SearchTypeError):
        await c.search("label", "q")


async def test_artist_albums_past_the_first_hundred_are_fetched():
    counts = {"album": 250, "ep": 120}

    async def reply(path, params=None, **_):
        if not path.endswith("/albums"):
            return {"name": "Test Artist"}
        kind = "ep" if params.get("filter") == "EPSANDSINGLES" else "album"
        start = params.get("offset", 0)
        ids = range(start, min(start + 100, counts[kind]))
        return {
            "totalNumberOfItems": counts[kind],
            "items": [{"id": f"{kind}{i}"} for i in ids],
        }

    c = _client()
    c._api_request = AsyncMock(side_effect=reply)
    ids = [a["id"] for a in (await c.get_metadata("1", "artist"))["albums"]]
    assert ids == [f"album{i}" for i in range(250)] + [f"ep{i}" for i in range(120)]


def _a(id, title, tracks=12, explicit=True):
    return {"id": id, "title": title, "numberOfTracks": tracks, "explicit": explicit}


@pytest.mark.parametrize(
    ("albums", "prefer_explicit", "kept"),
    [
        # Tidal lists one album twice, clean and explicit.
        ([_a("1", "STANS", explicit=False), _a("2", "STANS")], True, ["2"]),
        ([_a("1", "STANS", explicit=False), _a("2", "STANS")], False, ["1", "2"]),
        # A single that shares its title with an album is another release.
        ([_a("1", "Houdini", tracks=1), _a("2", "Houdini")], True, ["1", "2"]),
    ],
)
async def test_an_artists_duplicate_albums(albums, prefer_explicit, kept):
    async def reply(path, params=None, **_):
        if not path.endswith("/albums"):
            return {"name": "Test Artist"}
        is_eps = params and params.get("filter") == "EPSANDSINGLES"
        return {"items": [] if is_eps else albums}

    c = _client()
    c._api_request = AsyncMock(side_effect=reply)
    resp = await c.get_metadata("1", "artist")
    ids = ArtistMetadata.from_resp(resp, "tidal", prefer_explicit).ids
    assert sorted(ids) == kept


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


@pytest.mark.parametrize("kind", ["tracks", "albums"])
async def test_favorites_are_paged_oldest_first(kind):
    total = 130

    async def api(path, params=None):
        assert path == f"users/42/favorites/{kind}"
        assert (params["order"], params["orderDirection"]) == ("DATE", "ASC")
        offset = params.get("offset", 0)
        return {
            "totalNumberOfItems": total,
            "items": [
                {"item": {"id": n}} for n in range(offset, min(offset + 100, total))
            ],
        }

    c = _client()
    c.config.user_id = 42
    c._api_request = AsyncMock(side_effect=api)
    resp = await c.get_metadata(
        f"favorites:{kind}", "playlist" if kind == "tracks" else "label"
    )
    listed = resp["tracks"] if kind == "tracks" else resp["albums"]
    assert [i["id"] for i in listed] == list(range(total))


async def test_a_single_is_tagged_with_its_albums_own_artists():
    # A feature used to file the whole album under "Megan Thee Stallion, Spiritbox".
    album = {
        "id": 2,
        "title": "MEGAN: ACT II",
        "cover": None,
        "allowStreaming": True,
        "audioQuality": "LOSSLESS",
        "releaseDate": "2024-10-25",
        "numberOfTracks": 18,
        "numberOfVolumes": 1,
        "artists": [{"id": 9, "name": "Megan Thee Stallion"}],
    }
    track = {
        "id": 8,
        "title": "TYG (feat. Spiritbox)",
        "allowStreaming": True,
        "trackNumber": 8,
        "volumeNumber": 1,
        "streamStartDate": "2024-10-25T00:00:00.000+0000",
        "album": {"id": 2, "title": "MEGAN: ACT II", "cover": None},
        "artist": {"id": 9, "name": "Megan Thee Stallion"},
        "artists": [
            {"id": 9, "name": "Megan Thee Stallion"},
            {"id": 7, "name": "Spiritbox"},
        ],
    }

    async def reply(path, params=None, base=None):
        if path.startswith("albums/"):
            return dict(album)
        if path.endswith(("/lyrics", "/credits")):
            raise ItemNotFoundError("none")
        return dict(track)

    c = _client()
    c._api_request = AsyncMock(side_effect=reply)
    resp = await c.get_metadata("8", "track")
    album_meta = AlbumMetadata.from_track_resp(resp, "tidal")
    track_meta = TrackMetadata.from_tidal(album_meta, resp)

    assert album_meta.albumartists == ["Megan Thee Stallion"]
    assert (album_meta.tracktotal, album_meta.date) == (18, "2024-10-25")
    assert track_meta.artists == ["Megan Thee Stallion", "Spiritbox"]


@pytest.mark.parametrize(
    ("resp", "plain", "expected"),
    [
        ({"lyrics": "la", "subtitles": "[00:01.00] la"}, False, "[00:01.00] la"),
        ({"lyrics": "la", "subtitles": "[00:01.00] la"}, True, "la"),  # mp3
        (ItemNotFoundError("404"), False, ""),  # most tracks: no error
    ],
)
async def test_lyrics_are_synced_where_tidal_has_them(resp, plain, expected):
    c = _client()
    c._api_request = AsyncMock(side_effect=[resp])
    assert await c.get_lyrics("1", plain) == expected
