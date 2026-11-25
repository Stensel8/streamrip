"""Spotify: the client against a local server that plays Spotify's API, its
responses as tags, its links, and the login."""

import asyncio
import base64
import hashlib
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import responses
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
from streamrip.metadata import (
    AlbumMetadata,
    Covers,
    SearchResults,
    TrackMetadata,
)
from streamrip.metadata.util import get_album_track_ids
from streamrip.rip import prompter as prompter_module
from streamrip.rip import spotify_login
from streamrip.rip.parse_url import parse_url
from streamrip.rip.prompter import SpotifyPrompter
from streamrip.rip.spotify_login import (
    SpotifyLoginError,
    capture_code,
    code_from_redirect_url,
)
from streamrip.sync.auth import ConfigTokenStore, StoredToken
from streamrip.sync.providers.spotify import SpotifyOAuth

TRACK = "2A2b3C4d5E6f7G8h9I0jKl"
ALBUM = "1A2b3C4d5E6f7G8h9I0jKl"
ARTIST = "3A2b3C4d5E6f7G8h9I0jKl"
PLAYLIST = "4A2b3C4d5E6f7G8h9I0jKl"
PLAYLIST_ITEMS = f"/v1/playlists/{PLAYLIST}/items"

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
async def spotify(monkeypatch):
    """A running fake Spotify that the client and the login talk to."""
    fake = FakeSpotify()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    host, port = runner.addresses[0][:2]
    fake.base = f"http://{host}:{port}"
    monkeypatch.setattr(spotify_module, "API_URL", f"{fake.base}/v1")
    monkeypatch.setattr(spotify_module, "TOKEN_URL", f"{fake.base}/token")
    # A 429 would otherwise pause the test for seconds.
    monkeypatch.setattr("streamrip.client.client.retry_after", lambda *_: 0.01)
    monkeypatch.setattr("streamrip.client.client.random.random", lambda: 0.0)
    monkeypatch.setattr("streamrip.client.client.SLOWDOWN_START", 0.01)
    yield fake
    await runner.cleanup()


@pytest.fixture
async def make_client(spotify):
    """Build a client for the fake server, with the options given as keywords."""
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


def config_with_login(tmp_path, **login) -> Config:
    """A config file with a Spotify login; the file is what other programs read."""
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    config = Config(path)
    config.update_logins(
        "spotify",
        {
            "client_id": "CID",
            "access_token": "A",
            "refresh_token": "R",
            "token_expiry": str(int(time.time()) + 3600),
        }
        | login,
    )
    return config


# The login of the client


@pytest.mark.login
async def test_login_without_a_saved_login_asks_for_one(make_client):
    client = make_client(refresh_token="", access_token="", token_expiry="")

    with pytest.raises(MissingCredentialsError, match="Not logged in"):
        await client.login()


async def test_a_login_without_an_app_of_ones_own_asks_for_one(make_client):
    client = make_client(client_id="")  # what the pair link left behind

    with pytest.raises(MissingCredentialsError, match="app of your own"):
        await client.login()


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


@pytest.mark.login
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (
            (400, {"error": "invalid_grant", "error_description": "Token revoked"}),
            "Token revoked",
        ),
        ((503, None), "having trouble"),
    ],
    ids=["refused", "server in trouble"],
)
async def test_a_refresh_that_fails_says_so(make_client, spotify, answer, message):
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
    assert parts.netloc == "accounts.spotify.com"
    assert query["client_id"] == "CID"
    assert query["redirect_uri"] == "http://127.0.0.1:9900/callback"
    assert query["state"] == state
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert query["code_challenge"] == challenge


@pytest.mark.login
async def test_finishing_the_login_trades_the_code_for_tokens(make_client, spotify):
    spotify.on("POST", "/token", token_answer(refresh_token="FIRST"))
    client = make_client(access_token="", refresh_token="", token_expiry="")
    client.logged_in = False

    await client.finish_login("THE-CODE", "THE-VERIFIER")

    [call] = spotify.requests("/token")
    assert call["form"]["grant_type"] == "authorization_code"
    assert call["form"]["code"] == "THE-CODE"
    assert call["form"]["code_verifier"] == "THE-VERIFIER"
    assert client.logged_in
    assert client.config.refresh_token == "FIRST"
    assert client.global_config.file.spotify.access_token == "NEW"


async def test_a_refresh_is_saved_at_once(spotify, tmp_path):
    # Saved only at the end of a run, the old refresh token stayed in the file
    # and the next refresh spent it again.
    spotify.on("POST", "/token", token_answer(refresh_token="R2"))
    client = SpotifyClient(config_with_login(tmp_path, token_expiry="1"))

    await client.login()

    saved = Config(tmp_path / "config.toml").file.spotify
    assert (saved.access_token, saved.refresh_token) == ("NEW", "R2")
    await client.close()


async def test_a_login_another_program_refreshed_is_used_as_it_is(spotify, tmp_path):
    client = SpotifyClient(config_with_login(tmp_path, token_expiry="1"))
    Config(tmp_path / "config.toml").update_logins(
        "spotify",
        {
            "access_token": "ELSEWHERE",
            "refresh_token": "R3",
            "token_expiry": str(int(time.time()) + 3600),
        },
    )

    assert await client._fresh_token() == "ELSEWHERE"
    assert spotify.requests("/token") == []  # the new refresh token is not spent


async def test_a_refresh_outrun_by_another_program_takes_its_login(spotify, tmp_path):
    client = SpotifyClient(config_with_login(tmp_path, token_expiry="1"))
    other = Config(tmp_path / "config.toml")

    def rotated_meanwhile(_request):
        other.update_logins(
            "spotify",
            {
                "access_token": "A3",
                "refresh_token": "R3",
                "token_expiry": str(int(time.time()) + 3600),
            },
        )
        return 400, {"error": "invalid_grant"}  # the refresh token we sent is spent

    spotify.on("POST", "/token", rotated_meanwhile)

    assert await client._fresh_token() == "A3"


@responses.activate
def test_the_playlist_tools_refresh_the_shared_login_once(tmp_path):
    config = config_with_login(tmp_path, client_id="MYAPP")
    store = ConfigTokenStore(config)
    first, second = (
        StoredToken(SpotifyOAuth("MYAPP", "http://127.0.0.1:9900/callback"), store)
        for _ in range(2)
    )
    assert first() == second() == "A"
    responses.post(
        spotify_module.TOKEN_URL,
        json={"access_token": "A2", "refresh_token": "R2", "expires_in": 100},
    )

    assert first(force_refresh=True) == "A2"
    assert second(force_refresh=True) == "A2"  # takes the pair first saved
    assert len(responses.calls) == 1
    assert ConfigTokenStore(Config(tmp_path / "config.toml")).load("spotify")


# Requests


REJECTED = (401, {"error": {"message": "The access token expired"}})


@pytest.mark.login
async def test_a_rejected_token_is_replaced_once(make_client, spotify):
    spotify.on("POST", "/token", token_answer())
    spotify.on("GET", f"/v1/tracks/{TRACK}", [REJECTED, track_obj()])

    track = await make_client().get_metadata(TRACK, "track")

    assert track["id"] == TRACK
    sent = [c["auth"] for c in spotify.requests(f"/v1/tracks/{TRACK}")]
    assert sent == ["Bearer TOKEN", "Bearer NEW"]


@pytest.mark.login
async def test_a_token_that_stays_rejected_asks_for_a_new_login(make_client, spotify):
    spotify.on("POST", "/token", token_answer())
    spotify.on("GET", f"/v1/tracks/{TRACK}", REJECTED)

    with pytest.raises(AuthenticationError, match="Log in again"):
        await make_client().get_metadata(TRACK, "track")


PREMIUM = (
    403,
    {"error": {"message": "Active premium subscription required for the owner."}},
)
PLAYLIST_FOUND = {f"/v1/playlists/{PLAYLIST}": {"id": PLAYLIST, "name": "Not mine"}}


@pytest.mark.parametrize(
    ("answers", "media_type", "error", "says"),
    [
        ({f"/v1/tracks/{TRACK}": PREMIUM}, "track", SpotifyAPIError, "User Management"),
        ({}, "track", ItemNotFoundError, None),
        (
            {f"/v1/tracks/{TRACK}": (410, {"error": {"message": "gone"}})},
            "track",
            SpotifyAPIError,
            "Do not retry now",
        ),
        # Only playlists the user owns or collaborates on can be read.
        (
            PLAYLIST_FOUND | {PLAYLIST_ITEMS: (403, {"error": {"message": "no"}})},
            "playlist",
            NonStreamableError,
            PLAYLIST_NOT_READABLE,
        ),
    ],
    ids=["premium", "missing", "gone (410)", "not my playlist"],
)
async def test_what_spotify_refuses_is_explained(
    make_client, spotify, answers, media_type, error, says
):
    for path, answer in answers.items():
        spotify.on("GET", path, answer)
    item = TRACK if media_type == "track" else PLAYLIST

    with pytest.raises(error) as caught:
        await make_client().get_metadata(item, media_type)

    assert says is None or says in str(caught.value)


async def test_a_rate_limit_waits_once_and_says_so(make_client, spotify):
    spotify.on("GET", f"/v1/tracks/{TRACK}", (429, {"error": {"message": "slow"}}))
    client = make_client()

    with pytest.raises(SpotifyAPIError, match="too many requests"):
        await client.get_metadata(TRACK, "track")
    # One wait and one retry, not minutes of pausing; and no ban guard for a limit
    # that is the app's.
    assert len(spotify.requests(f"/v1/tracks/{TRACK}")) == 2
    assert not client._paced_owner()._limited_at


@pytest.mark.parametrize(
    "given",
    [TRACK, f"spotify:track:{TRACK}", f"https://open.spotify.com/track/{TRACK}?si=a"],
)
def test_an_id_is_taken_from_a_uri_or_a_link(given):
    assert plain_id(given) == TRACK


# Metadata


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

    album = await make_client().get_metadata(ALBUM, "album")

    assert [t["id"] for t in album["tracks"]] == ["t1", "t2"]
    assert get_album_track_ids("spotify", album) == ["t1", "t2"]


async def test_a_playlist_gives_its_tracks_from_items(make_client, spotify):
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
            ],
            "next": None,
        },
    )
    client = make_client()

    playlist = await client.get_metadata(PLAYLIST, "playlist")

    assert playlist["name"] == "CC Mix"
    assert [t["id"] for t in playlist["tracks"]] == ["new", "old"]
    # Its tracks come with it: a track of the playlist costs no request.
    await client.get_metadata("new", "track")
    assert spotify.requests("/v1/tracks/new") == []


@pytest.mark.parametrize(
    "link", ["http://localhost:1/v1/next", "{base}.evil.example/v1/next"]
)
async def test_a_next_page_link_off_the_api_is_not_followed(make_client, spotify, link):
    # The access token goes along with every request: not to an address that
    # only a response says is the next page.
    spotify.on("GET", f"/v1/artists/{ARTIST}", {"id": ARTIST, "name": "Kevin MacLeod"})
    spotify.on(
        "GET",
        f"/v1/artists/{ARTIST}/albums",
        {"items": [{"id": "a1"}], "next": link.format(base=spotify.base)},
    )

    with pytest.raises(APIError, match="next page link"):
        await make_client().get_metadata(ARTIST, "artist")


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

    artist = await make_client().get_metadata(ARTIST, "artist")

    assert artist["name"] == "Kevin MacLeod"
    assert [a["id"] for a in artist["albums"]] == ["a1", "a2", "a3"]
    # Spotify answers "Invalid limit" to more than 10 for an app in development mode.
    first = spotify.requests(f"/v1/artists/{ARTIST}/albums")[0]["query"]
    assert first["limit"] == "10"


# Search


@pytest.mark.parametrize(
    ("results", "limit", "asked", "found"),
    [
        (90, 25, [(10, 0), (10, 10), (5, 20)], 25),
        (3, 50, [(10, 0)], 3),
    ],
    ids=["ten at a time", "until there is no more"],
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
    assert sum(len(p["items"]) for p in pages) == found


async def test_search_results_fill_the_menu(make_client, spotify):
    found = {
        "track": track_obj(),
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
            f"{r.query['type']}s": {"items": [found[r.query["type"]]], "next": None}
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
    assert track.image_url == "https://i.scdn.co/image/640"
    playlist = await result("playlist")
    assert playlist.summarize() == "CC Mix by someone"
    assert ("Tracks", "3") in playlist.details
    assert playlist.description == "a mix & more"


# Audio


async def test_the_audio_is_the_match_on_youtube_music(make_client, spotify):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj(explicit=True))
    client = make_client(audio_format="mp3", audio_bitrate=192)
    found = AudioMatch(MatchTrack("Sneaky Snitch", video_id="vid123"), 0.97)
    client.matcher = SimpleNamespace(find=AsyncMock(return_value=found))

    downloadable = await client.get_downloadable(TRACK, 0)

    assert isinstance(downloadable, YtDlpDownloadable)
    assert downloadable.url == "https://music.youtube.com/watch?v=vid123"
    assert (downloadable.extension, downloadable.bitrate) == ("mp3", 192)
    wanted, isrc = client.matcher.find.await_args.args
    assert (wanted.title, wanted.artists) == ("Sneaky Snitch", ["Kevin MacLeod"])
    assert (wanted.duration_ms, wanted.explicit) == (135_000, True)
    assert isrc == "USABC1100001"


async def test_a_track_without_a_match_cannot_be_downloaded(make_client, spotify):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj())
    client = make_client()
    client.matcher = SimpleNamespace(find=AsyncMock(return_value=None))

    with pytest.raises(NonStreamableError, match="No match on YouTube Music"):
        await client.get_downloadable(TRACK, 0)


# Spotify's responses as tags

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
            {"text": "℗ 2013 Daft Life Limited", "type": "P"},
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

    assert (meta.album, meta.albumartist, meta.year) == (
        "Random Access Memories",
        "Daft Punk",
        "2013",
    )
    assert (meta.tracktotal, meta.info.container) == (13, "AAC")
    assert (meta.info.bit_depth, meta.info.sampling_rate) == (None, None)
    assert meta.get_copyright() == (
        "© 2013 Daft Life Limited; ℗ 2013 Daft Life Limited"
    )
    # So its folder has no empty quality bracket.
    folder = meta.format_folder_path(
        "{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]"
    )
    assert folder == Path("Daft Punk - Random Access Memories (2013) [AAC]")


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
    # The album of a lone track knows at least the disc that track is on.
    meta = AlbumMetadata.from_track_resp(track_resp(disc_number=2), "spotify")
    assert meta.disctotal == 2


@pytest.mark.parametrize(
    ("images", "biggest"), [(IMAGES, 640), (IMAGES[:1], 64), ([], None)]
)
def test_the_covers_take_the_biggest_image(images, biggest):
    covers = Covers.from_spotify({"images": images})

    if biggest is None:
        assert covers.empty()
    else:
        assert covers.get_size("original")[1].endswith(f"/{biggest}")


def test_a_track_is_tagged_from_spotifys_response():
    album = AlbumMetadata.from_track_resp(track_resp(), "spotify")

    meta = TrackMetadata.from_resp(album, "spotify", track_resp())

    assert meta.title == "Get Lucky"
    assert meta.artists == ["Daft Punk", "Pharrell Williams"]
    assert (meta.tracknumber, meta.discnumber) == (8, 1)
    assert meta.isrc == "GBDUM1300019"
    assert meta.info.explicit is True


def test_a_track_without_an_isrc_or_artists_still_has_tags():
    resp = track_resp(artists=[], external_ids={})
    album = AlbumMetadata.from_track_resp(resp, "spotify")

    meta = TrackMetadata.from_resp(album, "spotify", resp)

    assert meta.isrc is None
    assert meta.artist == "Daft Punk"  # the album's


# Links


@pytest.mark.parametrize(
    ("url", "media_type"),
    [
        (f"https://open.spotify.com/track/{ID}?si=0123456789abcdef", "track"),
        (f"https://open.spotify.com/intl-nl/album/{ID}", "album"),
        (f"https://open.spotify.com/embed/playlist/{ID}", "playlist"),
        (f"https://open.spotify.com/user/someone/playlist/{ID}", "playlist"),
        (f"spotify:track:{ID}", "track"),
        (f"spotify:user:someone:playlist:{ID}", "playlist"),
        # Not Spotify items:
        ("https://open.spotify.com/track/tooshort", None),
        (f"https://open.spotify.com/show/{ID}", None),  # podcasts: no tracks to match
        (f"https://example.com/track/{ID}", None),
    ],
)
def test_spotify_links_and_uris_are_recognised(url, media_type):
    parsed = parse_url(url)

    if media_type is None:
        assert parsed is None
    else:
        assert parsed.source == "spotify"
        assert parsed.match.groups() == (media_type, ID)


# The [spotify] config and its prompter

AUTHORIZE = "https://accounts.spotify.com/authorize?x"
REDIRECT = "http://127.0.0.1:9900/callback"  # the default redirect_uri
PASTED = f"{REDIRECT}?code=C&state=S"
TOKEN = spotify_module.Token("A", "R", time.time() + 3600)


@pytest.mark.login
def test_spotify_credentials_are_written_when_the_config_is_saved(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(path)

    with Config(path) as config:
        config.file.spotify.client_id = "abc"
        config.file.spotify.refresh_token = "r"
        config.file.set_modified()

    # The login is in state.toml, the setting stays in config.toml.
    state = tomlkit.parse((tmp_path / "state.toml").read_text())
    assert state["spotify"] == {"client_id": "abc", "refresh_token": "r"}
    assert "client_id" not in tomlkit.parse(path.read_text())["spotify"]


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


def ask_with(monkeypatch, *given) -> list[str]:
    """Answer Prompt.ask with `given`, in order; returns the questions asked."""
    queue, questions = list(given), []

    def ask(question, **_):
        questions.append(question)
        return queue.pop(0)

    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(ask))
    return questions


async def test_the_client_id_of_an_app_is_asked_when_it_is_not_saved(
    prompter, monkeypatch
):
    prompter.config.session.spotify.client_id = ""
    monkeypatch.setattr(prompter_module, "_open_login_link", lambda url: None)
    ask_with(monkeypatch, "  my-client-id  ", "2", PASTED)

    await prompter.prompt_and_login()

    assert prompter.config.session.spotify.client_id == "my-client-id"
    prompter.client.finish_login.assert_awaited_once_with("C", "V")


@pytest.mark.login
async def test_the_browser_login_catches_the_code_itself(prompter, monkeypatch):
    opened = []
    monkeypatch.setattr(prompter_module, "_open_login_link", opened.append)
    questions = ask_with(monkeypatch, "1")
    monkeypatch.setattr(
        prompter_module, "capture_code", AsyncMock(return_value="CAUGHT")
    )

    await prompter.prompt_and_login()

    assert opened == [AUTHORIZE]
    prompter.client.finish_login.assert_awaited_once_with("CAUGHT", "V")
    assert all("Client ID" not in question for question in questions)  # it is saved


@pytest.mark.login
@pytest.mark.parametrize(("screen", "default"), [(True, "1"), (False, "2")])
async def test_without_a_screen_the_other_device_is_the_default(
    prompter, monkeypatch, screen, default
):
    # A browser elsewhere can't reach the 127.0.0.1 Spotify sends it back to.
    monkeypatch.setattr(prompter_module, "has_screen", lambda: screen)
    defaults = []

    def ask(question, default=None, **_):
        defaults.append(default)
        return "2" if question == "Choose" else PASTED

    monkeypatch.setattr(prompter_module.Prompt, "ask", staticmethod(ask))

    await prompter.prompt_and_login()

    assert defaults[0] == default  # the first question is the browser choice
    prompter.client.finish_login.assert_awaited_once_with("C", "V")


@pytest.mark.login
@pytest.mark.parametrize("again", [True, False], ids=["tried again", "given up"])
async def test_a_failed_login_can_be_tried_again(prompter, monkeypatch, again):
    ask_with(monkeypatch, "2", "bad", "2", "good")
    monkeypatch.setattr(prompter_module.Confirm, "ask", lambda *a, **k: again)
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
    session.refresh_token, session.soloist_api_key = "refresh", "KEY"

    prompter.save()

    saved = config.file.spotify
    assert (saved.client_id, saved.access_token) == ("cid", "access")
    assert (saved.refresh_token, saved.soloist_api_key) == ("refresh", "KEY")
    assert config.file.modified


# The browser login: the redirect caught on a local port, or pasted


@pytest.mark.login
@pytest.mark.parametrize(
    ("address", "says"),
    [
        (f" {REDIRECT}?code=abc%2F123&state=s1 ", None),
        (f"{REDIRECT}?error=access_denied&state=s1", "access_denied"),
        (f"{REDIRECT}?code=abc&state=someone-elses", "state mismatch"),
        (REDIRECT, "no login in it"),
    ],
    ids=["ok", "refused", "another login's", "nothing pasted"],
)
def test_the_code_in_a_pasted_address(address, says):
    if says is None:
        assert code_from_redirect_url(address, "s1") == "abc/123"
    else:
        with pytest.raises(SpotifyLoginError, match=says):
            code_from_redirect_url(address, "s1")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
async def test_the_code_comes_back_through_the_local_port():
    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    async def favicon_then_login():
        # Another page the browser asks for does not end the wait.
        with pytest.raises(urllib.error.HTTPError, match="404"):
            await browse(f"http://127.0.0.1:{port}/favicon.ico")
        return await browse(f"{redirect_uri}?code=the-code&state=s1")

    code, page = await asyncio.gather(
        capture_code(redirect_uri, "s1"), favicon_then_login()
    )

    assert code == "the-code"
    assert b"You can close this tab" in page


@pytest.mark.login
async def test_a_connection_that_says_nothing_does_not_hold_up_the_login(monkeypatch):
    # A browser opens spare connections and can leave one unused. The server
    # takes one connection at a time, and used to wait on that one for good.
    monkeypatch.setattr(spotify_login._CallbackHandler, "timeout", 0.2)
    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    async def idle_then_login():
        with pytest.raises(urllib.error.HTTPError, match="404"):
            await browse(f"http://127.0.0.1:{port}/favicon.ico")  # it is listening
        with socket.create_connection(("127.0.0.1", port)):
            return await browse(f"{redirect_uri}?code=the-code&state=s1")

    code, _ = await asyncio.gather(capture_code(redirect_uri, "s1"), idle_then_login())

    assert code == "the-code"


@pytest.mark.login
@pytest.mark.parametrize(
    ("where", "message"),
    [
        ("a busy port", "Cannot listen"),
        ("https://example.com/callback", "only catch a login on this computer"),
    ],
)
async def test_a_login_that_cannot_be_caught_says_why(where, message):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        redirect_uri = where if "://" in where else f"http://127.0.0.1:{port}/callback"

        with pytest.raises(SpotifyLoginError, match=message):
            await capture_code(redirect_uri, "s1")


@pytest.mark.login
async def test_a_cancelled_login_lets_go_of_the_port(monkeypatch):
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
