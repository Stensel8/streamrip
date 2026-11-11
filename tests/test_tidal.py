import asyncio
import base64
import logging
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.client.downloadable import SegmentedMP4Downloadable, TidalDownloadable
from streamrip.client.tidal import DEFAULT_CLIENT_ID, HIRES_CLIENT_ID, TidalClient
from streamrip.config import Config
from streamrip.exceptions import (
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from streamrip.metadata import AlbumMetadata, ArtistMetadata, TrackMetadata
from streamrip.metadata.util import tidal_quality_id
from streamrip.rip.prompter import TidalPrompter, _open_login_link

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
    """One lane (the lossless client) unless `hires`."""
    cfg = Config.defaults()
    cfg.session.tidal.hires_client = hires
    return TidalClient(cfg)


def _dash(quality="HI_RES_LOSSLESS", rate=96000) -> dict:
    return {
        "audioQuality": quality,
        "bitDepth": 24,
        "sampleRate": rate,
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


# --- lanes and logins ---------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "lanes"),
    [
        ({}, [DEFAULT_CLIENT_ID, HIRES_CLIENT_ID]),
        ({"hires_client": False}, [DEFAULT_CLIENT_ID]),
        ({"quality": 2}, [DEFAULT_CLIENT_ID]),  # streamrip --quality 2
        ({"client_id": "abc", "client_secret": "xyz"}, ["abc", HIRES_CLIENT_ID]),
    ],
)
def test_lanes(settings, lanes):
    cfg = Config.defaults()
    for name, value in settings.items():
        setattr(cfg.session.tidal, name, value)
    assert [lane.client_id for lane in TidalClient(cfg).lanes()] == lanes


@pytest.mark.login
def test_each_lane_keeps_its_own_tokens():
    c = _client(hires=True)
    c.tokens.access_token = "lossless"
    c.hires_lane.tokens.access_token = "hires"
    assert (c.config.access_token, c.config.hires_access_token) == ("lossless", "hires")


@pytest.mark.login
async def test_token_from_other_client_requires_new_login(monkeypatch):
    c = _client()
    c.config.access_token = "token"
    c.config.token_client_id = HIRES_CLIENT_ID
    monkeypatch.setattr("streamrip.client.client.new_session", lambda **_: MagicMock())
    with pytest.raises(MissingCredentialsError):
        await c.login()


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
async def test_prompter_logs_both_lanes_in_and_saves_them(quiet_prompter):
    cfg = Config.defaults()
    client = TidalClient(cfg)
    for lane in client.lanes():
        _mock_login(lane)
    prompter = TidalPrompter(cfg, client)
    assert not prompter.has_creds()

    await prompter.prompt_and_login()
    prompter.save()  # as Main does after every prompt_and_login

    assert prompter.has_creds()
    t, saved = cfg.session.tidal, cfg.file.tidal
    assert (t.access_token, t.token_client_id) == (
        f"at-{DEFAULT_CLIENT_ID}",
        DEFAULT_CLIENT_ID,
    )
    assert (t.hires_access_token, t.hires_token_client_id) == (
        f"at-{HIRES_CLIENT_ID}",
        HIRES_CLIENT_ID,
    )
    assert (saved.access_token, saved.hires_access_token, saved.user_id) == (
        t.access_token,
        t.hires_access_token,
        1,
    )


@pytest.mark.login
async def test_prompter_leaves_a_working_login_alone(quiet_prompter):
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


@pytest.mark.login
@pytest.mark.parametrize(
    ("expires_in", "status", "error"),
    [(0, 0, "expired"), (300, 1, "access_denied")],
    ids=["the link expired", "Tidal said no"],
)
async def test_device_login_that_fails_says_why(
    quiet_prompter, expires_in, status, error
):
    cfg = Config.defaults()
    client = TidalClient(cfg)
    _mock_login(client, expires_in, status)
    with pytest.raises(AuthenticationError, match=error):
        await TidalPrompter(cfg, client)._device_login(client)


@pytest.mark.login
async def test_device_login_survives_the_wall_clock_jumping_ahead(
    quiet_prompter, monkeypatch
):
    calls = []

    def wall_clock():  # NTP, or a laptop waking up
        calls.append(None)
        return 1000.0 if len(calls) == 1 else 1000.0 + 3600

    monkeypatch.setattr("streamrip.rip.prompter.time.time", wall_clock)
    cfg = Config.defaults()
    client = TidalClient(cfg)
    _mock_login(client)

    await TidalPrompter(cfg, client)._device_login(client)
    assert client.logged_in


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


@pytest.mark.login
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="xdg-open is Linux's")
def test_the_browser_does_not_print_over_the_login_prompt(monkeypatch, tmp_path, capfd):
    """A Chrome-family browser that is already running answers on the terminal."""
    done = tmp_path / "done"
    xdg_open = tmp_path / "xdg-open"
    xdg_open.write_text(
        f"#!/bin/sh\necho 'Opening in existing browser session.'\ntouch '{done}'\n"
    )
    xdg_open.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    _open_login_link("https://link.tidal.com/X")
    for _ in range(100):  # xdg-open runs on its own
        if done.exists():
            break
        time.sleep(0.05)

    assert done.exists()
    assert capfd.readouterr().out == ""


# --- streams ------------------------------------------------------------------


async def test_dash_manifest_becomes_segmented_downloadable():
    c = _client()
    c.session = MagicMock()
    c._api_request = AsyncMock(return_value=_dash())
    dl = await c.get_downloadable("1", 3)
    assert isinstance(dl, SegmentedMP4Downloadable)
    assert (dl.source, dl.extension) == ("tidal", "flac")
    assert dl.init_url == "https://sp.example/init.mp4"
    assert dl.segment_urls == [f"https://sp.example/{n}.mp4" for n in range(1, 5)]
    # 3 Mb/s for (3 x 384000 + 100000) / 96000 seconds, without a HEAD each.
    assert await dl.size() == 4_890_625


@pytest.mark.parametrize(
    ("served", "codec", "asked", "warns"),
    [
        ("LOSSLESS", "flac", 2, False),
        ("LOSSLESS", "flac", 3, False),  # 3 is "the best there is"
        ("HIGH", "mp4a.40.2", 2, True),
    ],
)
async def test_a_stream_warns_only_when_it_is_worse_than_asked(
    caplog, served, codec, asked, warns
):
    c = _client()
    c.session = MagicMock()
    c._api_request = AsyncMock(return_value=_bts(served, codec))
    dl = await c.get_downloadable("1", asked)
    assert isinstance(dl, TidalDownloadable)
    assert dl.extension == ("flac" if codec == "flac" else "m4a")
    warned = any(r.levelno >= logging.WARNING for r in caplog.records)
    assert warned is warns


def _two_lanes(hires_reply, lossless_reply) -> TidalClient:
    c = _client(hires=True)
    c.session = c.hires_lane.session = MagicMock()
    for lane, reply in ((c.hires_lane, hires_reply), (c, lossless_reply)):
        key = "side_effect" if isinstance(reply, Exception) else "return_value"
        lane._api_request = AsyncMock(**{key: reply})
    return c


NOT_ASKED = AssertionError("the hi-res client must not be asked")


@pytest.mark.parametrize(
    ("asked", "tags", "hires_reply", "kind"),
    [
        (3, None, _dash(), SegmentedMP4Downloadable),
        (3, None, _bts("HIGH", "mp4a.40.2"), TidalDownloadable),  # no hi-res master
        (3, None, RuntimeError("boom"), TidalDownloadable),
        (
            3,
            ["LOSSLESS", "HIRES_LOSSLESS"],
            _bts("HIGH", "mp4a.40.2"),
            TidalDownloadable,
        ),
        (2, None, NOT_ASKED, TidalDownloadable),  # CD quality
        (3, ["LOSSLESS"], NOT_ASKED, TidalDownloadable),  # tagged: no hi-res master
    ],
    ids=["hi-res", "no master", "hi-res fails", "tagged hi-res", "cd", "tagged cd"],
)
async def test_which_lane_serves_a_track(asked, tags, hires_reply, kind):
    lossless = NOT_ASKED if kind is SegmentedMP4Downloadable else _bts("LOSSLESS")
    c = _two_lanes(hires_reply, lossless)
    if tags is not None:
        c._note_hires_tags([{"id": 1, "mediaMetadata": {"tags": tags}}])
    dl = await c.get_downloadable("1", asked)
    assert isinstance(dl, kind)
    assert c.hires_lane._api_request.await_count == (hires_reply is not NOT_ASKED)


@pytest.mark.parametrize("hires", [False, True])
async def test_nothing_streamable_raises_tidals_message(hires):
    c = _client(hires=hires)
    for lane in c.lanes():
        lane._api_request = AsyncMock(
            return_value={"userMessage": "Asset is not ready"}
        )
    with pytest.raises(NonStreamableError, match="not ready"):
        await c.get_downloadable("1", 3)


def test_unknown_tidal_quality_does_not_crash():
    assert tidal_quality_id("HI_RES_LOSSLESS") == 3
    assert tidal_quality_id("SOMETHING_NEW") == 2
    assert tidal_quality_id(None) == 0


# --- the format shown in folder names -------------------------------------------


async def test_album_items_teach_which_tracks_have_no_hires_master():
    c = _client(hires=True)
    c._api_request = AsyncMock(
        return_value={
            "totalNumberOfItems": 2,
            "items": [
                {
                    "type": "track",
                    "item": {"id": 7, "mediaMetadata": {"tags": ["LOSSLESS"]}},
                },
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
        "items": [
            {"type": "track", "item": {"id": i, "album": {"id": 1}}} for i in (11, 12)
        ],
    }

    async def reply(path, *_, **__):
        return items if path.endswith("/items") else dict(album)

    return reply


HIRES = ["LOSSLESS", "HIRES_LOSSLESS"]
FORMAT = {"bitDepth": 24, "sampleRate": 96000}


@pytest.mark.parametrize(
    ("tags", "hires", "lookup", "expected"),
    [
        (HIRES, True, _dash(), FORMAT),
        (["LOSSLESS"], True, NOT_ASKED, None),
        (HIRES, True, RuntimeError("429"), None),  # only the default label
        (HIRES, False, NOT_ASKED, None),  # no hi-res client to ask
    ],
)
async def test_an_albums_real_format(tags, hires, lookup, expected):
    c = _client(hires=hires)
    c._api_request = _album_replies(tags)
    if hires:
        key = "side_effect" if isinstance(lookup, Exception) else "return_value"
        c.hires_lane._api_request = AsyncMock(**{key: lookup})
    album = await c.get_metadata("1", "album")
    assert album.get("streamQuality") == expected


def _single(tags: list[str], in_album_folder: bool = True) -> TidalClient:
    """A client with track 11, on album 1, which has the same tags."""
    c = _client(hires=True)
    c.global_config.session.filepaths.add_singles_to_folder = in_album_folder
    tagged = {"mediaMetadata": {"tags": tags}}

    async def reply(path, *_, **__):
        if path.startswith("tracks/"):
            return {"id": 11, "album": {"id": 1}, **tagged}
        return {"id": 1, **tagged}

    c._api_request = reply
    return c


async def test_hires_single_reports_its_real_format_for_the_folder_name():
    c = _single(["HIRES_LOSSLESS"])
    c.hires_lane._api_request = AsyncMock(return_value=_dash())
    track = await c.get_metadata("11", "track")
    assert track["album"]["streamQuality"] == FORMAT
    assert "streamQuality" not in c._albums["1"]  # shared with the album's tracks


@pytest.mark.parametrize(
    ("tags", "in_album_folder", "listed"),
    [
        (["LOSSLESS"], True, False),
        (["HIRES_LOSSLESS"], False, False),
        (["HIRES_LOSSLESS"], True, True),
    ],
    ids=["not hi-res", "no album folder", "from a listing"],
)
async def test_single_costs_no_format_lookup_when_its_name_has_no_use_for_it(
    tags, in_album_folder, listed
):
    c = _single(tags, in_album_folder)
    if listed:
        c._listed["11"] = await c._api_request("tracks/11")
    c.hires_lane._api_request = AsyncMock(side_effect=NOT_ASKED)
    track = await c.get_metadata("11", "track")
    assert "streamQuality" not in track["album"]


# --- listings -----------------------------------------------------------------


async def test_search_returns_its_one_page():
    c = _client()
    c._api_request = AsyncMock(return_value={"items": [{"id": 1}]})
    assert await c.search("track", "q", limit=1) == [{"items": [{"id": 1}]}]


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


def _a(id, title, tracks=12, explicit=True, quality=None):
    album = {"id": id, "title": title, "numberOfTracks": tracks, "explicit": explicit}
    return album | ({"audioQuality": quality} if quality else {})


@pytest.mark.parametrize(
    ("albums", "prefer_explicit", "kept"),
    [
        # Tidal lists one album twice, clean and explicit; also with its
        # edition in () on one and [] on the other.
        ([_a("1", "STANS", explicit=False), _a("2", "STANS")], True, ["2"]),
        (
            [
                _a("1", "Recovery (Deluxe)", explicit=False),
                _a("2", "Recovery [Deluxe]"),
            ],
            True,
            ["2"],
        ),
        # The same explicit master at two quality tiers.
        (
            [
                _a("1", "Coup", quality="LOSSLESS"),
                _a("2", "Coup", quality="HI_RES_LOSSLESS"),
            ],
            True,
            ["2"],
        ),
        # A single that shares its title with an album is another release.
        ([_a("1", "Houdini", tracks=1), _a("2", "Houdini")], True, ["1", "2"]),
        ([_a("1", "STANS", explicit=False), _a("2", "STANS")], False, ["1", "2"]),
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
    # The album, then one request per page of 100 -- no empty page past the end.
    assert c._api_request.await_count == 1 + 3


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
    assert c._api_request.await_count == 2
    if kind == "tracks":
        assert "7" in c._listed  # its tracks need no request of their own


async def test_an_album_listing_gives_ids_as_numbers():
    """The check for a favorites id once failed every track of an album on that."""
    c = _client()
    c._listed["7"] = {"id": 7, "album": {"id": 1}}
    c._albums["1"] = {"id": 1}
    track = await c.get_metadata(7, "track")
    assert track["id"] == 7


# --- a track's album ----------------------------------------------------------

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


def _track_replies(album_error: Exception | None = None, slow: bool = False):
    """A feature on someone's album, and the album requests made."""
    asked = []

    async def reply(path, params=None, base=None):
        if slow:  # as a request does: tracks resolving together all wait
            await asyncio.sleep(0)
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
            "album": {"id": 2, "title": "MEGAN: ACT II", "cover": None},
            "artist": {"id": 9, "name": "Megan Thee Stallion"},
            "artists": [
                {"id": 9, "name": "Megan Thee Stallion"},
                {"id": 7, "name": "Spiritbox"},
            ],
        }

    return reply, asked


async def test_a_single_is_tagged_with_its_albums_own_artists():
    """A feature used to file the whole album under "Megan Thee Stallion, Spiritbox"."""
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


@pytest.mark.parametrize("album_error", [None, ItemNotFoundError("gone")])
async def test_tracks_resolving_together_ask_for_their_album_once(album_error):
    c = _client()
    reply, asked = _track_replies(album_error, slow=True)
    c._api_request = AsyncMock(side_effect=reply)

    resps = await asyncio.gather(
        *(c.get_metadata(str(i), "track") for i in range(8, 28))
    )

    assert asked == ["albums/2"]
    if album_error:  # each still downloads, tagged from its own album stub
        album = AlbumMetadata.from_track_resp(resps[0], "tidal")
        assert album.albumartist == "Megan Thee Stallion, Spiritbox"


async def test_tracks_of_a_downloaded_album_need_no_request_of_their_own():
    c = _client()
    c._api_request = _album_replies(["LOSSLESS"])
    await c.get_metadata("1", "album")
    c._api_request = AsyncMock(side_effect=AssertionError("no request"))

    resp = await c.get_metadata("11", "track")

    assert resp["album"]["id"] == 1
    assert "tracks" not in resp["album"]


async def test_only_a_hires_track_keeps_the_cached_hires_format():
    c = _client()
    # Downloaded as a hi-res album earlier in the run, the cached album carries
    # the format of the track that was asked about.
    c._albums["2"] = MEGAN_ACT_II | {"streamQuality": FORMAT}
    reply, _ = _track_replies()
    tags = {"tracks/8": ["LOSSLESS"], "tracks/9": HIRES}

    async def tracks(path, params=None, base=None):
        # As Tidal answers: LOSSLESS for every track, hi-res only in the tags.
        resp = await reply(path, params, base)
        if path.startswith("tracks/") and not path.endswith("/lyrics"):
            resp["audioQuality"] = "LOSSLESS"
            if path in tags:
                resp["mediaMetadata"] = {"tags": tags[path]}
        return resp

    c._api_request = AsyncMock(side_effect=tracks)

    async def label(track_id):
        info = AlbumMetadata.from_track_resp(
            await c.get_metadata(track_id, "track"), "tidal"
        ).info
        return info.bit_depth, info.sampling_rate

    assert await label("8") == (16, 44.1)  # lossless only
    assert await label("9") == (24, 96)  # hi-res
    assert await label("10") == (24, 96)  # no tag data: could be hi-res


@pytest.mark.parametrize(
    ("resp", "plain", "expected"),
    [
        ({"lyrics": "la", "subtitles": "[00:01.00] la"}, False, "[00:01.00] la"),
        ({"lyrics": "la", "subtitles": "[00:01.00] la"}, True, "la"),  # mp3
        ({"lyrics": "la"}, False, "la"),
        (ItemNotFoundError("404"), False, ""),  # most tracks: no error
    ],
)
async def test_lyrics_are_synced_where_tidal_has_them(resp, plain, expected):
    c = _client()
    c._api_request = AsyncMock(side_effect=[resp])
    assert await c.get_lyrics("1", plain) == expected
    c._api_request.assert_awaited_once_with(
        "tracks/1/lyrics", base="https://tidal.com/v1"
    )
