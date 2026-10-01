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
from streamrip.exceptions import (
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from streamrip.metadata import AlbumMetadata, ArtistMetadata, TrackMetadata
from streamrip.metadata.util import tidal_quality_id
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


def _client(hires: bool = False) -> TidalClient:
    """One lane (the lossless client) unless `hires`, like a config with
    hires_client = false."""
    cfg = Config.defaults()
    cfg.session.tidal.hires_client = hires
    return TidalClient(cfg)


def test_default_quality_is_the_highest():
    tidal = Config.defaults().session.tidal
    assert tidal.quality == 3
    assert tidal.hires_client is True


def test_defaults_ask_the_hires_client_first_and_the_lossless_one_second():
    client = TidalClient(Config.defaults())
    assert client.client_id == DEFAULT_CLIENT_ID
    assert client.hires_lane.client_id == HIRES_CLIENT_ID
    assert client.lanes() == [client, client.hires_lane]


def test_no_hires_lane_without_hires_client():
    client = _client()
    assert client.hires_lane is None
    assert client.lanes() == [client]


def test_no_hires_login_when_hires_is_not_asked_for():
    cfg = Config.defaults()
    cfg.session.tidal.quality = 2  # e.g. `streamrip --quality 2`
    assert TidalClient(cfg).hires_lane is None


def test_each_lane_keeps_its_own_tokens():
    c = _client(hires=True)
    c.tokens.access_token = "lossless"
    c.hires_lane.tokens.access_token = "hires"
    assert (c.config.access_token, c.config.hires_access_token) == ("lossless", "hires")


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
async def test_dash_remux_strips_container_metadata(monkeypatch, tmp_path):
    """ffmpeg must not carry the source MP4's own tags into the output FLAC.

    Without -map_metadata -1 -fflags +bitexact, a stream-copy remux leaves
    major_brand/minor_version/compatible_brands and ffmpeg's own encoder
    stamp sitting in the output's tags -- meaningless there, and streamrip's
    own tagger writes the real tags right after this step anyway.
    """
    monkeypatch.setattr(
        "streamrip.client.downloadable.find_ffmpeg", lambda: "/usr/bin/ffmpeg"
    )

    class FakeResp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def read(self):
            return b"segment-bytes"

    class FakeSession:
        def get(self, url):
            return FakeResp()

    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    create_subprocess = AsyncMock(return_value=proc)
    monkeypatch.setattr(
        "streamrip.client.downloadable.asyncio.create_subprocess_exec",
        create_subprocess,
    )

    out_path = str(tmp_path / "out.flac")
    monkeypatch.setattr(
        "streamrip.client.downloadable.os.path.isfile",
        lambda p: p == out_path,
    )

    dl = TidalDASHDownloadable(
        FakeSession(),
        "https://sp.example/init.mp4",
        ["https://sp.example/1.mp4"],
        "flac",
    )
    await dl._download(out_path, lambda n: None)

    args = create_subprocess.call_args.args
    assert args[args.index("-map_metadata") + 1] == "-1"
    assert args[args.index("-fflags") + 1] == "+bitexact"


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


def _bts(quality: str, codec: str = "flac") -> dict:
    manifest = (
        f'{{"urls": ["https://x/y"], "codecs": "{codec}", "encryptionType": "NONE"}}'
    )
    return {
        "audioQuality": quality,
        "manifestMimeType": "application/vnd.tidal.bts",
        "manifest": base64.b64encode(manifest.encode()).decode(),
    }


def _two_lanes(hires_reply, lossless_reply) -> TidalClient:
    c = _client(hires=True)
    c.session = c.hires_lane.session = MagicMock()
    for lane, reply in ((c.hires_lane, hires_reply), (c, lossless_reply)):
        if isinstance(reply, Exception):
            lane._api_request = AsyncMock(side_effect=reply)
        else:
            lane._api_request = AsyncMock(return_value=reply)
    return c


@pytest.mark.asyncio
async def test_hires_is_taken_when_tidal_has_it():
    hires = {
        "audioQuality": "HI_RES_LOSSLESS",
        "manifestMimeType": "application/dash+xml",
        "manifest": base64.b64encode(MPD.encode()).decode(),
    }
    c = _two_lanes(hires, AssertionError("the lossless client must not be asked"))
    assert isinstance(await c.get_downloadable("1", 3), TidalDASHDownloadable)


@pytest.mark.asyncio
async def test_no_hires_master_steps_down_to_the_lossless_client():
    # The hi-res client is only ever served AAC for such a track.
    c = _two_lanes(_bts("HIGH", "mp4a.40.2"), _bts("LOSSLESS"))
    dl = await c.get_downloadable("1", 3)
    assert isinstance(dl, TidalDownloadable)
    assert dl.extension == "flac"
    c.hires_lane._api_request.assert_awaited_once()
    c._api_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_hires_request_falls_back_and_says_so(caplog):
    c = _two_lanes(RuntimeError("boom"), _bts("LOSSLESS"))
    dl = await c.get_downloadable("1", 3)
    assert dl.extension == "flac"
    assert "hi-res request failed (boom); using lossless" in caplog.text


@pytest.mark.asyncio
async def test_hires_client_is_not_asked_below_quality_3():
    c = _two_lanes(AssertionError("not for CD quality"), _bts("LOSSLESS"))
    assert (await c.get_downloadable("1", 2)).extension == "flac"
    c.hires_lane._api_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_track_without_the_hires_tag_never_asks_the_hires_client():
    c = _two_lanes(AssertionError("no hi-res master, so not asked"), _bts("LOSSLESS"))
    c._note_hires_tags([{"id": 1, "mediaMetadata": {"tags": ["LOSSLESS"]}}])
    assert (await c.get_downloadable("1", 3)).extension == "flac"
    c.hires_lane._api_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "track",
    [
        {"id": 1, "mediaMetadata": {"tags": ["LOSSLESS", "HIRES_LOSSLESS"]}},
        {"id": 1},  # no tag data: still asked, so hi-res is never missed
    ],
)
async def test_hires_tagged_or_untagged_tracks_are_asked(track):
    c = _two_lanes(_bts("HIGH", "mp4a.40.2"), _bts("LOSSLESS"))
    c._note_hires_tags([track])
    await c.get_downloadable("1", 3)
    c.hires_lane._api_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_album_items_teach_which_tracks_have_no_hires_master():
    c = _client(hires=True)
    tagged = {"id": 7, "mediaMetadata": {"tags": ["LOSSLESS"]}}
    c._api_request = AsyncMock(
        return_value={
            "totalNumberOfItems": 2,
            "items": [
                {"type": "track", "item": tagged},
                {"type": "track", "item": {"id": 8}},
            ],
        }
    )
    await c._get_tracks("albums/1")
    assert c._no_hires == {"7"}


def _album_replies(tags: list[str]):
    """An album with two tracks, as `_api_request` would answer for it."""
    album = {"id": 1, "mediaMetadata": {"tags": tags}}
    items = {
        "totalNumberOfItems": 2,
        "items": [{"type": "track", "item": {"id": i}} for i in (11, 12)],
    }

    async def reply(path, *_, **__):
        return items if path.endswith("/items") else dict(album)

    return reply


def _hires_stream(rate: int) -> dict:
    return {
        "audioQuality": "HI_RES_LOSSLESS",
        "bitDepth": 24,
        "sampleRate": rate,
        "manifestMimeType": "application/dash+xml",
        "manifest": base64.b64encode(MPD.encode()).decode(),
    }


@pytest.mark.asyncio
async def test_hires_album_reports_its_real_format_for_the_folder_name():
    c = _client(hires=True)
    c._api_request = _album_replies(["LOSSLESS", "HIRES_LOSSLESS"])
    c.hires_lane._api_request = AsyncMock(return_value=_hires_stream(96000))
    album = await c.get_metadata("1", "album")
    assert album["streamQuality"] == {"bitDepth": 24, "sampleRate": 96000}
    c.hires_lane._api_request.assert_awaited_once()  # one request for the album


@pytest.mark.asyncio
async def test_ordinary_album_costs_no_extra_request_and_keeps_its_label():
    c = _client(hires=True)
    c._api_request = _album_replies(["LOSSLESS"])
    c.hires_lane._api_request = AsyncMock(side_effect=AssertionError("not hi-res"))
    assert "streamQuality" not in await c.get_metadata("1", "album")


@pytest.mark.asyncio
async def test_album_format_lookup_failure_only_leaves_the_default_label():
    c = _client(hires=True)
    c._api_request = _album_replies(["LOSSLESS", "HIRES_LOSSLESS"])
    c.hires_lane._api_request = AsyncMock(side_effect=RuntimeError("429"))
    assert "streamQuality" not in await c.get_metadata("1", "album")


@pytest.mark.asyncio
async def test_no_format_lookup_without_the_hires_client():
    c = _client()
    c._api_request = _album_replies(["LOSSLESS", "HIRES_LOSSLESS"])
    assert "streamQuality" not in await c.get_metadata("1", "album")


@pytest.mark.asyncio
async def test_nothing_streamable_anywhere_still_raises():
    c = _two_lanes({"userMessage": "Asset is not ready"}, {"userMessage": "nope"})
    with pytest.raises(NonStreamableError, match="nope"):
        await c.get_downloadable("1", 3)


@pytest.mark.asyncio
async def test_single_search_hit_is_returned():
    c = _client()
    c._api_request = AsyncMock(return_value={"items": [{"id": 1}]})
    assert await c.search("track", "q", limit=1) == [{"items": [{"id": 1}]}]


def _artist_replies(albums, eps=None):
    """Tidal splits an artist's discography across two endpoints; both are
    plain {"items": [...]} lists of album summaries."""

    async def reply(path, params=None, **_):
        if path.endswith("/albums"):
            is_eps = params and params.get("filter") == "EPSANDSINGLES"
            return {"items": (eps or []) if is_eps else albums}
        return {"name": "Test Artist"}

    return reply


async def _artist(albums, prefer_explicit=True):
    """What the shared artist step keeps of this Tidal discography."""
    c = _client()
    c._api_request = AsyncMock(side_effect=_artist_replies(albums))
    resp = await c.get_metadata("1", "artist")
    kept = set(ArtistMetadata.from_resp(resp, "tidal", prefer_explicit).ids)
    return {"albums": [a for a in resp["albums"] if a["id"] in kept]}


@pytest.mark.asyncio
async def test_artist_albums_drop_the_clean_copy_of_an_explicit_duplicate():
    """Tidal sometimes lists one album twice: once clean, once explicit."""
    albums = [
        {"id": "1", "title": "STANS", "numberOfTracks": 12, "explicit": False},
        {"id": "2", "title": "STANS", "numberOfTracks": 12, "explicit": True},
    ]
    artist = await _artist(albums)
    assert [a["id"] for a in artist["albums"]] == ["2"]


@pytest.mark.asyncio
async def test_artist_albums_keep_the_higher_quality_copy_when_both_explicit():
    """Tidal also lists the same explicit master at two quality tiers."""
    albums = [
        {
            "id": "1",
            "title": "Coup De Grâce",
            "numberOfTracks": 19,
            "explicit": True,
            "audioQuality": "LOSSLESS",
        },
        {
            "id": "2",
            "title": "Coup De Grâce",
            "numberOfTracks": 19,
            "explicit": True,
            "audioQuality": "HI_RES_LOSSLESS",
        },
    ]
    artist = await _artist(albums)
    assert [a["id"] for a in artist["albums"]] == ["2"]


@pytest.mark.asyncio
async def test_artist_albums_ignore_bracket_style_when_matching_titles():
    """Tidal tags the same edition "(Deluxe Edition)" on one release and
    "[Deluxe Edition]" on another -- still the same album.
    """
    albums = [
        {
            "id": "1",
            "title": "Recovery (Deluxe Edition)",
            "numberOfTracks": 19,
            "explicit": False,
        },
        {
            "id": "2",
            "title": "Recovery [Deluxe Edition]",
            "numberOfTracks": 19,
            "explicit": True,
        },
    ]
    artist = await _artist(albums)
    assert [a["id"] for a in artist["albums"]] == ["2"]


@pytest.mark.asyncio
async def test_artist_albums_with_different_track_counts_are_not_merged():
    """A single sharing a title with an album isn't the same release."""
    albums = [
        {"id": "1", "title": "Houdini", "numberOfTracks": 1, "explicit": True},
        {"id": "2", "title": "Houdini", "numberOfTracks": 12, "explicit": True},
    ]
    artist = await _artist(albums)
    assert {a["id"] for a in artist["albums"]} == {"1", "2"}


@pytest.mark.asyncio
async def test_artist_album_duplicates_kept_when_prefer_explicit_is_off():
    albums = [
        {"id": "1", "title": "STANS", "numberOfTracks": 12, "explicit": False},
        {"id": "2", "title": "STANS", "numberOfTracks": 12, "explicit": True},
    ]
    artist = await _artist(albums, prefer_explicit=False)
    assert [a["id"] for a in artist["albums"]] == ["1", "2"]


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


def _mock_login(lane: TidalClient) -> None:
    lane.session = MagicMock()
    lane._get_device_code = AsyncMock(return_value=("code", "link.tidal.com/X", 300))
    lane._get_auth_status = AsyncMock(
        return_value=(
            0,
            {
                "user_id": 1,
                "country_code": "NL",
                "access_token": f"at-{lane.client_id}",
                "refresh_token": f"rt-{lane.client_id}",
                "token_expiry": 123.0,
            },
        )
    )


@pytest.mark.asyncio
async def test_prompter_logs_both_lanes_in_and_saves_them(monkeypatch):
    monkeypatch.setattr("streamrip.rip.prompter.launch", lambda *_: None)
    monkeypatch.setattr("streamrip.rip.prompter.console", MagicMock())
    cfg = Config.defaults()
    client = TidalClient(cfg)
    for lane in client.lanes():
        _mock_login(lane)
    prompter = TidalPrompter(cfg, client)
    assert not prompter.has_creds()

    await prompter.prompt_and_login()

    assert prompter.has_creds()
    t = cfg.session.tidal
    assert (t.access_token, t.token_client_id) == (
        f"at-{DEFAULT_CLIENT_ID}",
        DEFAULT_CLIENT_ID,
    )
    assert (t.hires_access_token, t.hires_token_client_id) == (
        f"at-{HIRES_CLIENT_ID}",
        HIRES_CLIENT_ID,
    )
    saved = cfg.file.tidal  # what gets written to config.toml
    assert saved.access_token == t.access_token
    assert saved.hires_access_token == t.hires_access_token
    assert saved.user_id == 1


@pytest.mark.asyncio
async def test_prompter_leaves_a_working_login_alone(monkeypatch):
    monkeypatch.setattr("streamrip.rip.prompter.launch", lambda *_: None)
    monkeypatch.setattr("streamrip.rip.prompter.console", MagicMock())
    cfg = Config.defaults()
    client = TidalClient(cfg)
    _mock_login(client)
    client.hires_lane.tokens.access_token = "still-good"
    client.hires_lane._login_lane = AsyncMock()  # its saved login works
    client.hires_lane._get_device_code = AsyncMock()

    await TidalPrompter(cfg, client).prompt_and_login()

    client.hires_lane._get_device_code.assert_not_called()
    assert cfg.session.tidal.hires_access_token == "still-good"
    assert cfg.session.tidal.access_token == f"at-{DEFAULT_CLIENT_ID}"


@pytest.mark.asyncio
async def test_device_login_stops_when_tidal_lets_the_link_expire(monkeypatch):
    # Tidal keeps a device code valid for expiresIn seconds (300). Waiting
    # longer only ever got its error back, reported as a rejected login.
    monkeypatch.setattr("streamrip.rip.prompter.launch", lambda *_: None)
    monkeypatch.setattr("streamrip.rip.prompter.console", MagicMock())
    cfg = Config.defaults()
    client = TidalClient(cfg)
    _mock_login(client)
    client._get_device_code = AsyncMock(return_value=("code", "link.tidal.com/X", 0))

    with pytest.raises(AuthenticationError, match="expired"):
        await TidalPrompter(cfg, client)._device_login(client)
    client._get_auth_status.assert_not_called()


@pytest.mark.asyncio
async def test_device_login_rejection_says_why(monkeypatch):
    monkeypatch.setattr("streamrip.rip.prompter.launch", lambda *_: None)
    monkeypatch.setattr("streamrip.rip.prompter.console", MagicMock())
    cfg = Config.defaults()
    client = TidalClient(cfg)
    _mock_login(client)
    client._get_auth_status = AsyncMock(return_value=(1, {"error": "access_denied"}))

    with pytest.raises(AuthenticationError, match="access_denied"):
        await TidalPrompter(cfg, client)._device_login(client)


MEGAN_ACT_II = {
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


def _track_replies(album_error: Exception | None = None, album_id: int = 2):
    """A feature on someone's album, and how many times the album was asked."""
    asked = []

    async def reply(path, params=None, base=None):
        if path.startswith("albums/"):
            asked.append(path)
            if album_error:
                raise album_error
            return dict(MEGAN_ACT_II)
        if path.endswith("/lyrics"):
            raise ItemNotFoundError("no lyrics")
        return {
            "id": int(path.split("/")[1]),
            "title": "TYG (feat. Spiritbox)",
            "allowStreaming": True,
            "trackNumber": 8,
            "volumeNumber": 1,
            "streamStartDate": "2024-10-25T00:00:00.000+0000",
            "album": {"id": album_id, "title": "MEGAN: ACT II", "cover": None},
            "artist": {"id": 9, "name": "Megan Thee Stallion"},
            "artists": [
                {"id": 9, "name": "Megan Thee Stallion"},
                {"id": 7, "name": "Spiritbox"},
            ],
        }

    return reply, asked


@pytest.mark.asyncio
async def test_a_single_is_tagged_with_its_albums_own_artists():
    # The track's own artists used to stand in for the album's, so a
    # feature filed the whole album under "Megan Thee Stallion, Spiritbox".
    c = _client()
    reply, asked = _track_replies()
    c._api_request = AsyncMock(side_effect=reply)

    resp = await c.get_metadata("8", "track")
    album = AlbumMetadata.from_track_resp(resp, "tidal")
    track = TrackMetadata.from_tidal(album, resp)

    assert (album.albumartist, album.albumartists) == (
        "Megan Thee Stallion",
        ["Megan Thee Stallion"],
    )
    assert (album.tracktotal, album.date) == (18, "2024-10-25")
    assert track.artists == ["Megan Thee Stallion", "Spiritbox"]
    await c.get_metadata("9", "track")  # another track of the same album
    assert asked == ["albums/2"]


@pytest.mark.asyncio
async def test_a_single_whose_album_cannot_be_fetched_still_downloads():
    c = _client()
    reply, _ = _track_replies(album_error=ItemNotFoundError("gone"))
    c._api_request = AsyncMock(side_effect=reply)

    resp = await c.get_metadata("8", "track")
    album = AlbumMetadata.from_track_resp(resp, "tidal")

    # As before: the album stub, filled in from the track.
    assert album.albumartist == "Megan Thee Stallion, Spiritbox"


@pytest.mark.asyncio
async def test_tracks_of_a_downloaded_album_need_no_album_request():
    c = _client()
    c._api_request = _album_replies(["LOSSLESS"])
    await c.get_metadata("1", "album")
    reply, asked = _track_replies(album_id=1)
    c._api_request = AsyncMock(side_effect=reply)

    resp = await c.get_metadata("11", "track")

    assert asked == []
    assert resp["album"]["id"] == 1
    assert "tracks" not in resp["album"]
