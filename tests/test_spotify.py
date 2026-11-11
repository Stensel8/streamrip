"""Spotify: the client against a local server that plays Spotify's API, its
responses as tags, its links, and the login."""

import asyncio
import base64
import hashlib
import logging
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import tomlkit
from aiohttp import web

from streamrip.client import spotify as spotify_module
from streamrip.client.audio_match import MatchTrack
from streamrip.client.downloadable import YtDlpDownloadable
from streamrip.client.spotify import (
    PLAYLIST_NOT_READABLE,
    SpotifyAPIError,
    SpotifyClient,
    plain_id,
)
from streamrip.client.ytmusic import AudioMatch
from streamrip.config import Config, set_user_defaults
from streamrip.exceptions import (
    APIError,
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from streamrip.media import PendingAlbum, PendingArtist, PendingPlaylist, PendingSingle
from streamrip.metadata import (
    AlbumMetadata,
    ArtistMetadata,
    Covers,
    PlaylistMetadata,
    SearchResults,
    TrackMetadata,
)
from streamrip.metadata.util import get_album_track_ids
from streamrip.rip import prompter as prompter_module
from streamrip.rip import spotify_login
from streamrip.rip.parse_url import parse_url
from streamrip.rip.prompter import PROMPTERS, SpotifyPrompter, get_prompter
from streamrip.rip.spotify_login import (
    SpotifyLoginError,
    capture_code,
    code_from_redirect_url,
)

TRACK = "2A2b3C4d5E6f7G8h9I0jKl"
ALBUM = "1A2b3C4d5E6f7G8h9I0jKl"
ARTIST = "3A2b3C4d5E6f7G8h9I0jKl"
PLAYLIST = "4A2b3C4d5E6f7G8h9I0jKl"

IMAGES = [
    {"url": "https://i.scdn.co/image/64", "width": 64},
    {"url": "https://i.scdn.co/image/640", "width": 640},
    {"url": "https://i.scdn.co/image/300", "width": 300},
]
ARTISTS = [{"id": ARTIST, "name": "Kevin MacLeod"}]
ALBUM_SIMPLE = {
    "id": ALBUM,
    "name": "Sneaky Snitch",
    "artists": ARTISTS,
    "release_date": "2011-02-01",
    "images": IMAGES,
    "total_tracks": 2,
    "album_type": "single",
}


def track_obj(track_id=TRACK, **extra):
    return {
        "id": track_id,
        "type": "track",
        "name": "Sneaky Snitch",
        "artists": ARTISTS,
        "album": ALBUM_SIMPLE,
        "duration_ms": 135_000,
        "track_number": 1,
        "disc_number": 1,
        "explicit": False,
        "external_ids": {"isrc": "usabc1100001"},
    } | extra


class FakeSpotify:
    """Answers by (method, path); a list of answers is used up one by one."""

    def __init__(self):
        self.answers: dict[tuple[str, str], object] = {}
        self.calls: list[dict] = []

    def on(self, method, path, answer):
        self.answers[(method, path)] = answer

    def requests(self, path):
        return [c for c in self.calls if c["path"] == path]

    async def handle(self, request: web.Request) -> web.Response:
        form = dict(await request.post()) if request.method == "POST" else {}
        self.calls.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "auth": request.headers.get("Authorization"),
                "form": form,
            }
        )
        answer = self.answers.get((request.method, request.path))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if answer is None:
            return web.json_response(
                {"error": {"message": "no such thing"}}, status=404
            )
        if callable(answer):
            answer = answer(request)
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        return web.json_response(body, status=status)


@pytest.fixture
async def spotify():
    """A running fake Spotify; its base url is `.base`."""
    fake = FakeSpotify()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    host, port = runner.addresses[0][:2]
    fake.base = f"http://{host}:{port}"
    yield fake
    await runner.cleanup()


@pytest.fixture
async def make_client(spotify, monkeypatch):
    """Build a client for the fake server, with the options given as keywords."""
    monkeypatch.setattr(spotify_module, "API_URL", f"{spotify.base}/v1")
    monkeypatch.setattr(spotify_module, "TOKEN_URL", f"{spotify.base}/token")
    # A 429 would otherwise pause the test for seconds.
    monkeypatch.setattr("streamrip.client.client.retry_after", lambda *_: 0.01)
    clients = []

    def make(**options):
        config = Config.defaults()
        config.session.downloads.requests_per_minute = -1
        defaults = {
            "client_id": "CID",
            "refresh_token": "REFRESH",
            "access_token": "TOKEN",
            "token_expiry": str(int(time.time()) + 3600),
        }
        for name, value in (defaults | options).items():
            setattr(config.session.spotify, name, value)
        client = SpotifyClient(config)
        client._ensure_session()
        client.logged_in = True
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.session.close()


def token_answer(**fields):
    return {"access_token": "NEW", "expires_in": 3600, "token_type": "Bearer"} | fields


# --- the client's login ------------------------------------------------------


@pytest.mark.login
@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ({"client_id": ""}, "client id"),
        (
            {"refresh_token": "", "access_token": "", "token_expiry": ""},
            "Not logged in",
        ),
    ],
)
async def test_login_without_an_app_or_a_saved_login_asks_for_one(
    make_client, missing, message
):
    with pytest.raises(MissingCredentialsError, match=message):
        await make_client(**missing).login()


@pytest.mark.login
async def test_a_saved_token_that_still_lasts_is_used_as_it_is(make_client, spotify):
    client = make_client()
    client.logged_in = False

    await client.login()

    assert client.logged_in
    assert spotify.calls == []


@pytest.mark.login
@pytest.mark.parametrize(
    ("expiry", "answer", "refresh"),
    [
        (str(int(time.time()) - 5), token_answer(refresh_token="ROTATED"), "ROTATED"),
        # Spotify only sends a new refresh token when it rotates them.
        ("", token_answer(), "REFRESH"),
    ],
    ids=["expired", "no expiry"],
)
async def test_a_stale_token_is_refreshed_and_kept(
    make_client, spotify, expiry, answer, refresh
):
    spotify.on("POST", "/token", answer)
    client = make_client(token_expiry=expiry)
    client.logged_in = False

    await client.login()

    assert client.logged_in
    [call] = spotify.requests("/token")
    assert call["form"] == {
        "grant_type": "refresh_token",
        "refresh_token": "REFRESH",
        "client_id": "CID",
    }
    for saved in (client.config, client.global_config.file.spotify):
        assert (saved.access_token, saved.refresh_token) == ("NEW", refresh)
        assert float(saved.token_expiry) > time.time() + 3000
    assert client.global_config.file.modified


@pytest.mark.login
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (
            (
                400,
                {
                    "error": "invalid_grant",
                    "error_description": "Refresh token revoked",
                },
            ),
            "Refresh token revoked",
        ),
        ((502, None), None),  # "null": aiohttp reads None
    ],
    ids=["refused", "no body"],
)
async def test_a_refresh_spotify_refuses_asks_for_a_new_login(
    make_client, spotify, answer, message
):
    spotify.on("POST", "/token", answer)
    client = make_client(token_expiry="")

    with pytest.raises(AuthenticationError, match=message):
        await client.login()


@pytest.mark.login
async def test_the_login_url_carries_a_pkce_challenge_for_its_verifier(make_client):
    client = make_client(redirect_uri="http://127.0.0.1:9900/callback")

    url, state, verifier = client.authorization_url()

    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://accounts.spotify.com/authorize"
    )
    assert query["client_id"] == "CID"
    assert query["redirect_uri"] == "http://127.0.0.1:9900/callback"
    assert query["response_type"] == "code"
    assert query["state"] == state
    assert query["code_challenge_method"] == "S256"
    assert set(query["scope"].split()) == {
        "playlist-read-private",
        "playlist-read-collaborative",
    }
    digest = hashlib.sha256(verifier.encode()).digest()
    assert (
        query["code_challenge"]
        == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    )


@pytest.mark.login
async def test_finishing_the_login_trades_the_code_for_tokens(make_client, spotify):
    spotify.on("POST", "/token", token_answer(refresh_token="FIRST"))
    client = make_client(access_token="", refresh_token="", token_expiry="")
    client.logged_in = False

    await client.finish_login("THE-CODE", "THE-VERIFIER")

    [call] = spotify.requests("/token")
    assert call["form"] == {
        "grant_type": "authorization_code",
        "code": "THE-CODE",
        "redirect_uri": client.config.redirect_uri,
        "client_id": "CID",
        "code_verifier": "THE-VERIFIER",
    }
    assert client.logged_in
    assert client.config.refresh_token == "FIRST"
    assert client.global_config.file.spotify.access_token == "NEW"


# --- requests ----------------------------------------------------------------


@pytest.mark.login
@pytest.mark.parametrize("again", [False, True], ids=["new token works", "not"])
async def test_a_rejected_token_is_replaced_once(make_client, spotify, again):
    rejected = (401, {"error": {"message": "The access token expired"}})
    spotify.on("POST", "/token", token_answer())
    spotify.on(
        "GET", f"/v1/tracks/{TRACK}", [rejected, rejected if again else track_obj()]
    )
    client = make_client()

    if again:
        with pytest.raises(AuthenticationError, match="Log in again"):
            await client.get_metadata(TRACK, "track")
    else:
        assert (await client.get_metadata(TRACK, "track"))["id"] == TRACK

    assert [c["auth"] for c in spotify.requests(f"/v1/tracks/{TRACK}")] == [
        "Bearer TOKEN",
        "Bearer NEW",
    ]
    assert len(spotify.requests("/token")) == 1


PREMIUM = (
    403,
    {"error": {"message": "Active premium subscription required for the owner."}},
)
PLAYLIST_FOUND = {f"/v1/playlists/{PLAYLIST}": {"id": PLAYLIST, "name": "Not mine"}}
PLAYLIST_ITEMS = f"/v1/playlists/{PLAYLIST}/items"


@pytest.mark.parametrize(
    ("answers", "media_type", "error", "says", "tries"),
    [
        (
            {f"/v1/tracks/{TRACK}": PREMIUM},
            "track",
            SpotifyAPIError,
            "User Management",  # how to get past the rule
            1,
        ),
        ({}, "track", ItemNotFoundError, None, 1),
        (
            {f"/v1/tracks/{TRACK}": (429, {"error": {"message": "slow down"}})},
            "track",
            APIError,
            "too many requests",
            4,  # the usual retries
        ),
        # Only playlists the user owns or collaborates on can be read.
        (
            PLAYLIST_FOUND | {PLAYLIST_ITEMS: (403, {"error": {"message": "no"}})},
            "playlist",
            NonStreamableError,
            PLAYLIST_NOT_READABLE,
            2,
        ),
        (
            PLAYLIST_FOUND | {PLAYLIST_ITEMS: (404, {"error": {"message": "no"}})},
            "playlist",
            NonStreamableError,
            PLAYLIST_NOT_READABLE,
            2,
        ),
        (
            {f"/v1/playlists/{PLAYLIST}": PREMIUM},
            "playlist",
            SpotifyAPIError,
            "User Management",
            1,
        ),
    ],
    ids=[
        "premium",
        "missing",
        "rate limited",
        "not my playlist (403)",
        "not my playlist (404)",
        "premium, not a private playlist",
    ],
)
async def test_what_spotify_refuses_is_explained(
    make_client, spotify, answers, media_type, error, says, tries
):
    for path, answer in answers.items():
        spotify.on("GET", path, answer)
    item = TRACK if media_type == "track" else PLAYLIST

    with pytest.raises(error) as caught:
        await make_client().get_metadata(item, media_type)

    assert says is None or says in str(caught.value)
    assert len(spotify.calls) == tries


@pytest.mark.parametrize(
    "given",
    [
        TRACK,
        f"spotify:track:{TRACK}",
        f"https://open.spotify.com/track/{TRACK}?si=abc",
        f"https://open.spotify.com/track/{TRACK}/",
    ],
)
def test_an_id_is_taken_from_a_uri_or_a_link(given):
    assert plain_id(given) == TRACK


# --- metadata ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("audio_format", "container"), [("m4a", "AAC"), ("mp3", "MP3")]
)
async def test_a_track_is_fetched_once_and_knows_its_container(
    make_client, spotify, audio_format, container
):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj())
    client = make_client(audio_format=audio_format)

    first = await client.get_metadata(TRACK, "track")
    second = await client.get_metadata(f"spotify:track:{TRACK}", "track")

    assert first is second
    assert len(spotify.requests(f"/v1/tracks/{TRACK}")) == 1
    assert first["album"]["container"] == container


async def test_an_album_comes_with_all_of_its_tracks(make_client, spotify):
    second_page = f"{spotify.base}/v1/albums/{ALBUM}/tracks?offset=1&limit=1"
    spotify.on(
        "GET",
        f"/v1/albums/{ALBUM}",
        ALBUM_SIMPLE
        | {
            "tracks": {
                "items": [{"id": "t1", "disc_number": 1}, None],
                "next": second_page,
            }
        },
    )
    spotify.on(
        "GET",
        f"/v1/albums/{ALBUM}/tracks",
        {"items": [{"id": "t2", "disc_number": 2}, {"id": None}], "next": None},
    )
    client = make_client()

    album = await client.get_metadata(ALBUM, "album")

    assert [t["id"] for t in album["tracks"]] == ["t1", "t2"]
    assert get_album_track_ids("spotify", album) == ["t1", "t2"]
    assert album["container"] == "AAC"


async def test_a_playlist_gives_its_tracks_from_items(make_client, spotify, caplog):
    spotify.on("GET", f"/v1/playlists/{PLAYLIST}", {"id": PLAYLIST, "name": "CC Mix"})
    spotify.on(
        "GET",
        PLAYLIST_ITEMS,
        {
            "items": [
                {"item": track_obj("new")},  # how it is since February 2026
                {"track": track_obj("old")},  # how it was
                {"item": None},  # a track Spotify no longer has
                {"item": track_obj("local", is_local=True)},
                {"item": {"id": "ep", "type": "episode"}},
                None,
            ],
            "next": None,
        },
    )
    client = make_client()

    with caplog.at_level(logging.INFO, logger="streamrip"):
        playlist = await client.get_metadata(PLAYLIST, "playlist")

    assert playlist["name"] == "CC Mix"
    assert [t["id"] for t in playlist["tracks"]] == ["new", "old"]
    assert spotify.requests(PLAYLIST_ITEMS)[0]["query"]["limit"] == "50"
    # The user is told how many were skipped: a gone track, a local file, an episode.
    assert "Skipped 3 items of CC Mix" in caplog.text


async def test_the_tracks_of_a_playlist_need_no_second_request(make_client, spotify):
    spotify.on("GET", f"/v1/playlists/{PLAYLIST}", {"id": PLAYLIST, "name": "Mix"})
    spotify.on(
        "GET", PLAYLIST_ITEMS, {"items": [{"item": track_obj("in-it")}], "next": None}
    )
    client = make_client()

    await client.get_metadata(PLAYLIST, "playlist")
    track = await client.get_metadata("in-it", "track")

    assert track["album"]["container"] == "AAC"
    assert spotify.requests("/v1/tracks/in-it") == []


async def test_an_artist_comes_with_its_own_releases(make_client, spotify):
    next_page = f"{spotify.base}/v1/artists/{ARTIST}/albums?offset=2"
    spotify.on("GET", f"/v1/artists/{ARTIST}", {"id": ARTIST, "name": "Kevin MacLeod"})
    spotify.on(
        "GET",
        f"/v1/artists/{ARTIST}/albums",
        [
            {"items": [{"id": "a1"}, {"id": "a2"}, None], "next": next_page},
            {"items": [{"id": "a2"}, {"id": "a3"}], "next": None},
        ],
    )
    client = make_client()

    artist = await client.get_metadata(ARTIST, "artist")

    assert artist["name"] == "Kevin MacLeod"
    assert [a["id"] for a in artist["albums"]] == ["a1", "a2", "a3"]
    first = spotify.requests(f"/v1/artists/{ARTIST}/albums")[0]["query"]
    assert first["include_groups"] == "album,single"
    # Spotify answers "Invalid limit" to more than 10 for an app in development mode.
    assert first["limit"] == "10"


async def test_labels_are_not_a_thing_on_spotify(make_client):
    client = make_client()

    with pytest.raises(ValueError, match="labels"):
        await client.get_metadata("x", "label")
    with pytest.raises(ValueError, match="labels"):
        await client.search("label", "x")


# --- search ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("results", "limit", "asked", "found"),
    [
        (90, 25, [(10, 0), (10, 10), (5, 20)], 25),
        (990, 500, [(10, n) for n in range(0, 50, 10)], 50),
        (3, 50, [(10, 0)], 3),
    ],
    ids=["ten at a time", "never more than fifty", "until there is no more"],
)
async def test_a_search_is_asked_in_pages(
    make_client, spotify, results, limit, asked, found
):
    def answer(request):
        offset, size = int(request.query["offset"]), int(request.query["limit"])
        items = [
            track_obj(f"id{n}") for n in range(offset, min(offset + size, results))
        ]
        more = offset + size < results
        return {"tracks": {"items": items, "next": "more" if more else None}}

    spotify.on("GET", "/v1/search", answer)

    pages = await make_client().search("track", "sneaky", limit=limit)

    queries = [c["query"] for c in spotify.requests("/v1/search")]
    assert [(int(q["limit"]), int(q["offset"])) for q in queries] == asked
    assert all(q["q"] == "sneaky" and q["type"] == "track" for q in queries)
    assert sum(len(p["items"]) for p in pages) == found


async def test_search_results_fill_the_menu(make_client, spotify):
    found = {
        "track": track_obj(),
        "album": ALBUM_SIMPLE | {"album_type": "album", "total_tracks": 12},
        "artist": {
            "id": ARTIST,
            "name": "Kevin MacLeod",
            "images": IMAGES,
            "followers": {"total": 5},
        },
        # Since February 2026 a playlist's "tracks" summary is called "items".
        "playlist": {
            "id": PLAYLIST,
            "name": "CC Mix",
            "owner": {"id": "u1", "display_name": "someone"},
            "items": {"total": 3},
            "description": "a mix &amp; more",
            "images": IMAGES,
        },
    }
    spotify.on(
        "GET",
        "/v1/search",
        lambda r: {
            f"{r.query['type']}s": {
                "items": [found[r.query["type"]], None],
                "next": None,
            }
        },
    )
    client = make_client()

    async def result(media_type):
        pages = await client.search(media_type, "x", limit=1)
        [only] = SearchResults.from_pages("spotify", media_type, pages).results
        return only

    track = await result("track")
    assert (track.id, track.media_type) == (TRACK, "track")
    assert track.summarize() == "Sneaky Snitch by Kevin MacLeod (2011)"
    assert ("Album", "Sneaky Snitch") in track.details
    assert track.image_url == "https://i.scdn.co/image/640"

    album = await result("album")
    assert album.summarize() == "Sneaky Snitch by Kevin MacLeod (2011)"
    assert ("Tracks", "12") in album.details
    assert ("Type", "Album") in album.details

    artist = await result("artist")
    assert artist.summarize() == "Kevin MacLeod"
    assert artist.image_url == "https://i.scdn.co/image/640"

    playlist = await result("playlist")
    assert playlist.summarize() == "CC Mix by someone"
    assert ("Tracks", "3") in playlist.details
    assert playlist.description == "a mix & more"


# --- audio -------------------------------------------------------------------


class RecordingMatcher:
    def __init__(self, result):
        self.result = result
        self.asked = []

    async def find(self, wanted, isrc=None):
        self.asked.append((wanted, isrc))
        return self.result


async def test_the_audio_is_the_match_on_youtube_music(make_client, spotify):
    spotify.on(
        "GET", f"/v1/tracks/{TRACK}", track_obj(explicit=True, name="Sneaky Snitch")
    )
    client = make_client(audio_format="MP3", audio_bitrate=192)
    found = AudioMatch(MatchTrack("Sneaky Snitch", video_id="vid123"), 0.97)
    client.matcher = RecordingMatcher(found)

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, YtDlpDownloadable)
    assert downloadable.url == "https://music.youtube.com/watch?v=vid123"
    assert downloadable.extension == "mp3"
    assert downloadable.bitrate == 192
    assert downloadable.source == "spotify"
    [(wanted, isrc)] = client.matcher.asked
    assert wanted.title == "Sneaky Snitch"
    assert wanted.artists == ["Kevin MacLeod"]
    assert wanted.album == "Sneaky Snitch"
    assert wanted.duration_ms == 135_000
    assert wanted.explicit is True
    assert isrc == "USABC1100001"


@pytest.mark.parametrize(
    ("audio_format", "found", "error", "message"),
    [
        ("m4a", None, NonStreamableError, "No match on YouTube Music"),
        (
            "flac",  # not one yt-dlp's audio is kept or encoded as
            AudioMatch(MatchTrack("Sneaky Snitch", video_id="v"), 1.0),
            ValueError,
            "m4a",
        ),
    ],
    ids=["no match", "wrong audio format"],
)
async def test_a_track_that_cannot_be_downloaded_says_why(
    make_client, spotify, audio_format, found, error, message
):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj())
    client = make_client(audio_format=audio_format)
    client.matcher = RecordingMatcher(found)

    with pytest.raises(error, match=message):
        await client.get_downloadable(TRACK, 0)


# --- Spotify's responses as tags ---------------------------------------------

ID = "4uLU6hMCjMI75M1A2tKUQC"


def album_resp(**extra):
    return {
        "id": "album1",
        "name": "Random Access Memories",
        "artists": [{"name": "Daft Punk"}],
        "release_date": "2013-05-17",
        "images": IMAGES,
        "total_tracks": 13,
        "genres": ["electronic"],
        "copyrights": [
            {"text": "2013 Daft Life Limited", "type": "C"},
            {"text": "\u2117 2013 Daft Life Limited", "type": "P"},
            {"text": "  ", "type": "C"},
        ],
        "container": "AAC",
    } | extra


def track_resp(**extra):
    return {
        "id": ID,
        "name": "Get Lucky ",
        "artists": [{"name": "Daft Punk"}, {"name": "Pharrell Williams"}],
        "album": album_resp(),
        "track_number": 8,
        "disc_number": 1,
        "explicit": True,
        "external_ids": {"isrc": "gbdum1300019"},
    } | extra


def test_an_album_is_lossy_and_says_which_container():
    meta = AlbumMetadata.from_album_resp(album_resp(), "spotify")

    assert meta is not None
    assert meta.album == "Random Access Memories"
    assert meta.albumartist == "Daft Punk"
    assert meta.albumartists == ["Daft Punk"]
    assert meta.year == "2013"
    assert meta.date == "2013-05-17"
    assert meta.genre == ["electronic"]
    assert meta.tracktotal == 13
    assert meta.info.id == "album1"
    assert meta.info.container == "AAC"
    assert meta.info.bit_depth is None
    assert meta.info.sampling_rate is None
    assert meta.copyright == "(C) 2013 Daft Life Limited; \u2117 2013 Daft Life Limited"
    assert (
        meta.get_copyright()
        == "\u00a9 2013 Daft Life Limited; \u2117 2013 Daft Life Limited"
    )
    # So its folder has no empty quality bracket.
    folder = meta.format_folder_path(
        "{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]"
    )
    assert folder == Path("Daft Punk - Random Access Memories (2013) [AAC]")

    resp = album_resp()
    del resp["container"]
    assert AlbumMetadata.from_album_resp(resp, "spotify").info.container == "AAC"


@pytest.mark.parametrize(
    ("date", "year"), [("2013", "2013"), ("2013-05", "2013"), (None, "Unknown")]
)
def test_a_release_date_can_be_a_year_only(date, year):
    meta = AlbumMetadata.from_album_resp(album_resp(release_date=date), "spotify")

    assert meta.year == year


def test_the_discs_of_an_album_come_from_its_tracks():
    tracks = [{"id": "a", "disc_number": 1}, {"id": "b", "disc_number": 2}]

    assert (
        AlbumMetadata.from_album_resp(album_resp(tracks=tracks), "spotify").disctotal
        == 2
    )
    assert AlbumMetadata.from_album_resp(album_resp(), "spotify").disctotal == 1
    # The album of a lone track knows at least the disc that track is on.
    meta = AlbumMetadata.from_track_resp(track_resp(disc_number=2), "spotify")
    assert (meta.disctotal, meta.album) == (2, "Random Access Memories")


@pytest.mark.parametrize(
    ("resp", "sizes"),
    [
        (
            {"images": IMAGES},
            {"original": 640, "large": 640, "small": 300, "thumbnail": 64},
        ),
        ({"images": IMAGES[:1]}, dict.fromkeys(Covers.SIZES, 64)),
        ({"images": []}, None),
        ({}, None),
    ],
    ids=["three sizes", "one image for all", "none", "no images at all"],
)
def test_the_covers_take_spotifys_sizes(resp, sizes):
    covers = Covers.from_spotify(resp)

    if sizes is None:
        assert covers.empty()
    else:
        assert {s: covers.get_size(s)[1] for s in Covers.SIZES} == {
            s: f"https://i.scdn.co/image/{px}" for s, px in sizes.items()
        }
        assert covers.largest()[1] == f"https://i.scdn.co/image/{sizes['original']}"


def test_a_track_is_tagged_from_spotifys_response():
    album = AlbumMetadata.from_track_resp(track_resp(), "spotify")

    meta = TrackMetadata.from_resp(album, "spotify", track_resp())

    assert meta is not None
    assert meta.title == "Get Lucky"
    assert meta.artist == "Daft Punk, Pharrell Williams"
    assert meta.artists == ["Daft Punk", "Pharrell Williams"]
    assert meta.tracknumber == 8
    assert meta.discnumber == 1
    assert meta.isrc == "GBDUM1300019"
    assert meta.info.id == ID
    assert meta.info.explicit is True


def test_a_track_without_an_isrc_or_artists_still_has_tags():
    resp = track_resp(artists=[], external_ids={})
    album = AlbumMetadata.from_track_resp(resp, "spotify")

    meta = TrackMetadata.from_resp(album, "spotify", resp)

    assert meta.isrc is None
    assert meta.artists is None
    assert meta.artist == "Daft Punk"  # the album's


@pytest.mark.parametrize(
    ("meta_class", "listed"),
    [(PlaylistMetadata, "tracks"), (ArtistMetadata, "albums")],
)
def test_a_playlist_or_artist_is_its_name_and_the_ids_it_lists(meta_class, listed):
    resp = {"name": "CC Mix", listed: [{"id": "a"}, {"id": "b"}]}

    meta = meta_class.from_resp(resp, "spotify")

    assert (meta.name, meta.ids) == ("CC Mix", ["a", "b"])


# --- urls --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "media_type"),
    [
        (f"https://open.spotify.com/track/{ID}", "track"),
        (f"https://open.spotify.com/track/{ID}?si=0123456789abcdef", "track"),
        (f"https://open.spotify.com/intl-nl/album/{ID}", "album"),
        (f"https://open.spotify.com/intl-pt-BR/artist/{ID}?si=x", "artist"),
        (f"https://open.spotify.com/embed/playlist/{ID}", "playlist"),
        (f"https://open.spotify.com/user/someone/playlist/{ID}", "playlist"),
        (f"http://open.spotify.com/album/{ID}", "album"),
        (f"spotify:track:{ID}", "track"),
        (f"spotify:user:someone:playlist:{ID}", "playlist"),
        (f"  https://open.spotify.com/track/{ID}\n", "track"),
        # Not Spotify items:
        ("https://open.spotify.com/track/tooshort", None),
        (f"https://open.spotify.com/track/{ID}x", None),  # 23 characters
        (f"https://open.spotify.com/show/{ID}", None),  # podcasts: no tracks to match
        (f"https://open.spotify.com/episode/{ID}", None),
        (f"https://example.com/track/{ID}", None),
        (f"spotify:show:{ID}", None),
        ("https://open.spotify.com/", None),
    ],
)
def test_spotify_links_and_uris_are_recognised(url, media_type):
    parsed = parse_url(url)

    if media_type is None:
        assert parsed is None
    else:
        assert parsed.source == "spotify"
        assert parsed.match.groups() == (media_type, ID)


@pytest.mark.parametrize(
    ("url", "pending_type"),
    [
        (f"https://open.spotify.com/track/{ID}", PendingSingle),
        (f"https://open.spotify.com/album/{ID}", PendingAlbum),
        (f"https://open.spotify.com/artist/{ID}", PendingArtist),
        (f"https://open.spotify.com/playlist/{ID}", PendingPlaylist),
    ],
)
async def test_a_spotify_url_becomes_the_pending_item_of_its_kind(url, pending_type):
    client = SimpleNamespace(source="spotify")

    pending = await parse_url(url).into_pending(client, Config.defaults(), None)

    assert isinstance(pending, pending_type)
    assert pending.id == ID
    assert pending.client is client


# --- the [spotify] config and its prompter -----------------------------------

AUTHORIZE = "https://accounts.spotify.com/authorize?x"
REDIRECT = "http://127.0.0.1:9900/callback"  # the default redirect_uri
PASTED = f"{REDIRECT}?code=C&state=S"


@pytest.mark.login
def test_spotify_credentials_are_written_when_the_config_is_saved(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(path)

    with Config(path) as config:
        config.file.spotify.client_id = "abc"
        config.file.spotify.refresh_token = "r"
        config.file.set_modified()

    saved = tomlkit.parse(path.read_text())
    assert saved["spotify"]["client_id"] == "abc"
    assert saved["spotify"]["refresh_token"] == "r"
    assert saved["spotify"]["audio_format"] == "m4a"


@pytest.fixture
def prompter(monkeypatch):
    """A prompter with a saved app, a quiet console and a client that only
    pretends to log in."""
    config = Config.defaults()
    config.session.spotify.client_id = "saved"
    client = SpotifyClient(config)
    client.authorization_url = lambda: (AUTHORIZE, "S", "V")
    client.finish_login = AsyncMock()
    monkeypatch.setattr(prompter_module.console, "print", lambda *a, **k: None)
    return SpotifyPrompter(config, client)


class Answers:
    """Plays back the answers to Prompt.ask, in order, and keeps the questions."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question, **_):
        self.questions.append(question)
        return self.answers.pop(0)


def _answer(monkeypatch, *answers) -> Answers:
    answers = Answers(*answers)
    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(answers))
    return answers


@pytest.mark.login
def test_the_prompter_is_registered_for_spotify(prompter):
    assert PROMPTERS["spotify"] is SpotifyPrompter
    assert isinstance(get_prompter(prompter.client, prompter.config), SpotifyPrompter)


@pytest.mark.login
@pytest.mark.parametrize(
    ("client_id", "refresh_token", "has"),
    [("", "", False), ("cid", "", False), ("", "r", False), ("cid", "r", True)],
)
def test_a_login_needs_the_app_and_a_refresh_token(
    prompter, client_id, refresh_token, has
):
    prompter.config.session.spotify.client_id = client_id
    prompter.config.session.spotify.refresh_token = refresh_token

    assert prompter.has_creds() is has


async def test_the_app_is_explained_then_the_client_id_asked(prompter, monkeypatch):
    prompter.config.session.spotify.client_id = ""
    printed = []
    monkeypatch.setattr(
        prompter_module.console, "print", lambda *a, **k: printed.append(a)
    )
    monkeypatch.setattr(prompter_module, "_open_login_link", lambda url: None)
    answers = _answer(monkeypatch, "  my-client-id  ", "2", PASTED)

    await prompter.prompt_and_login()

    assert prompter.config.session.spotify.client_id == "my-client-id"
    prompter.client.finish_login.assert_awaited_once_with("C", "V")
    explanation = " ".join(str(part) for call in printed for part in call)
    assert "Premium" in explanation
    assert "developer.spotify.com/dashboard" in explanation
    assert "Web API" in explanation
    assert REDIRECT in explanation
    assert answers.questions[0] == "Enter the Client ID of your Spotify app"


async def test_a_saved_client_id_is_not_asked_again(prompter, monkeypatch):
    answers = _answer(monkeypatch, "2", PASTED)

    await prompter.prompt_and_login()

    assert all("Client ID" not in question for question in answers.questions)


@pytest.mark.login
async def test_the_browser_login_catches_the_code_itself(prompter, monkeypatch):
    opened = []
    monkeypatch.setattr(prompter_module, "_open_login_link", opened.append)
    _answer(monkeypatch, "1")
    capture = AsyncMock(return_value="CAUGHT")
    monkeypatch.setattr(prompter_module, "capture_code", capture)

    await prompter.prompt_and_login()

    assert opened == [AUTHORIZE]
    capture.assert_awaited_once_with(REDIRECT, "S")
    prompter.client.finish_login.assert_awaited_once_with("CAUGHT", "V")


@pytest.mark.login
@pytest.mark.parametrize(("screen", "default"), [(True, "1"), (False, "2")])
async def test_without_a_screen_the_other_device_is_the_default(
    prompter, monkeypatch, screen, default
):
    """A browser elsewhere can't reach the 127.0.0.1 Spotify sends it back to."""
    monkeypatch.setattr(prompter_module, "has_screen", lambda: screen)
    defaults = []

    def ask(question, default=None, **_):
        defaults.append(default)
        return "2" if question == "Choose" else PASTED

    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(ask))

    await prompter.prompt_and_login()

    assert defaults[0] == default
    prompter.client.finish_login.assert_awaited_once_with("C", "V")


@pytest.mark.login
@pytest.mark.parametrize("again", [True, False], ids=["tried again", "given up"])
async def test_a_failed_login_can_be_tried_again(prompter, monkeypatch, again):
    _answer(monkeypatch, "2", "bad", "2", "good")
    monkeypatch.setattr(
        prompter_module.Confirm, "ask", staticmethod(lambda *a, **k: again)
    )
    monkeypatch.setattr(
        prompter_module,
        "code_from_redirect_url",
        MagicMock(side_effect=[SpotifyLoginError("no login in it"), "GOOD-CODE"]),
    )

    if again:
        await prompter.prompt_and_login()
        prompter.client.finish_login.assert_awaited_once_with("GOOD-CODE", "V")
    else:
        with pytest.raises(AuthenticationError, match="cancelled"):
            await prompter.prompt_and_login()


@pytest.mark.login
def test_saving_copies_the_app_and_the_login_to_the_file(prompter):
    config = prompter.config
    session = config.session.spotify
    session.client_id, session.access_token = "cid", "access"
    session.refresh_token, session.token_expiry = "refresh", "1900000000"

    prompter.save()

    saved = config.file.spotify
    assert (saved.client_id, saved.access_token) == ("cid", "access")
    assert (saved.refresh_token, saved.token_expiry) == ("refresh", "1900000000")
    assert config.file.modified


# --- the browser login: the redirect caught on a local port, or pasted -------


@pytest.mark.login
@pytest.mark.parametrize(
    ("address", "says"),
    [
        (f" {REDIRECT}?code=abc%2F123&state=s1 ", None),
        (f"{REDIRECT}?error=access_denied&state=s1", "access_denied"),
        (f"{REDIRECT}?code=abc&state=someone-elses", "state mismatch"),
        (f"{REDIRECT}?state=s1", "no authorization code"),
        (REDIRECT, "no login in it"),
        ("", "no login in it"),
    ],
    ids=["ok", "refused", "another login's", "no code", "no query", "nothing"],
)
def test_the_code_in_a_pasted_address(address, says):
    if says is None:
        assert code_from_redirect_url(address, "s1") == "abc/123"
    else:
        with pytest.raises(SpotifyLoginError, match=says):
            code_from_redirect_url(address, "s1")


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _ipv6_loopback() -> bool:
    try:
        free_port("::1")
    except OSError:
        return False
    return True


async def browse(url: str):
    """What the browser does after the login: GET the redirect URI."""
    # The server is started in a thread by capture_code: wait for its port.
    for _ in range(50):
        try:
            return await asyncio.to_thread(
                lambda: urllib.request.urlopen(url, timeout=5).read()
            )
        except urllib.error.HTTPError:
            raise
        except OSError:
            await asyncio.sleep(0.1)
    raise AssertionError("the login server never came up")


@pytest.mark.login
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        pytest.param(
            "::1",
            marks=pytest.mark.skipif(
                not _ipv6_loopback(), reason="no IPv6 loopback here"
            ),
        ),
    ],
)
async def test_the_code_comes_back_through_the_local_port(host):
    port = free_port(host)
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    redirect_uri = f"http://{netloc}/callback"

    async def favicon_then_login():
        # Another page the browser asks for does not end the wait.
        with pytest.raises(urllib.error.HTTPError, match="404"):
            await browse(f"http://{netloc}/favicon.ico")
        return await browse(f"{redirect_uri}?code=the-code&state=s1")

    code, page = await asyncio.gather(
        capture_code(redirect_uri, "s1"), favicon_then_login()
    )

    assert code == "the-code"
    assert b"You can close this tab" in page


@pytest.mark.login
async def test_a_login_for_another_attempt_is_not_accepted():
    redirect_uri = f"http://127.0.0.1:{free_port()}/callback"

    with pytest.raises(SpotifyLoginError, match="state mismatch"):
        await asyncio.gather(
            capture_code(redirect_uri, "s1"),
            browse(f"{redirect_uri}?code=the-code&state=other"),
        )


@pytest.mark.login
@pytest.mark.parametrize(
    ("where", "message"),
    [
        ("a free port", "Timed out"),
        ("a busy port", "Cannot listen"),
        ("https://example.com/callback", "only catch a login on this computer"),
    ],
)
async def test_a_login_that_cannot_be_caught_says_why(monkeypatch, where, message):
    monkeypatch.setattr(spotify_login, "LOGIN_TIMEOUT", 0.2)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1] if where == "a busy port" else free_port()
        redirect_uri = where if "://" in where else f"http://127.0.0.1:{port}/callback"

        with pytest.raises(SpotifyLoginError, match=message):
            await capture_code(redirect_uri, "s1")


@pytest.mark.login
async def test_a_cancelled_login_lets_go_of_the_port_at_once(monkeypatch):
    monkeypatch.setattr(spotify_login, "LOGIN_TIMEOUT", 30)
    port = free_port()
    waiting = asyncio.create_task(
        capture_code(f"http://127.0.0.1:{port}/callback", "s1")
    )
    await asyncio.sleep(0.3)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    # The listening thread stops within its 1-second poll, not after 30.
    for _ in range(30):
        try:
            socket.create_server(("127.0.0.1", port)).close()
            break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        raise AssertionError("the port is still taken 3 seconds after Ctrl-C")
