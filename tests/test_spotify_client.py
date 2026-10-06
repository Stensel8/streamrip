"""SpotifyClient against a local server that plays Spotify's API."""

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web

from streamrip.client import spotify as spotify_module
from streamrip.client.audio_match import MatchTrack
from streamrip.client.downloadable import YtDlpDownloadable
from streamrip.client.spotify import (
    PLAYLIST_NOT_READABLE,
    PREMIUM_REQUIRED,
    SpotifyAPIError,
    SpotifyClient,
    plain_id,
)
from streamrip.client.ytmusic import AudioMatch
from streamrip.config import Config
from streamrip.exceptions import (
    APIError,
    AuthenticationError,
    ItemNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from streamrip.metadata import SearchResults
from streamrip.metadata.util import get_album_track_ids

TRACK = "2A2b3C4d5E6f7G8h9I0jKl"
ALBUM = "1A2b3C4d5E6f7G8h9I0jKl"
ARTIST = "3A2b3C4d5E6f7G8h9I0jKl"
PLAYLIST = "4A2b3C4d5E6f7G8h9I0jKl"

IMAGES = [
    {"url": "https://i.scdn.co/image/small", "width": 64},
    {"url": "https://i.scdn.co/image/large", "width": 640},
    {"url": "https://i.scdn.co/image/medium", "width": 300},
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
        client.ensure_session()
        client.logged_in = True
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.session.close()


def token_answer(**fields):
    return {"access_token": "NEW", "expires_in": 3600, "token_type": "Bearer"} | fields


# --- login -------------------------------------------------------------------


async def test_login_without_a_client_id_asks_for_one(make_client):
    client = make_client(client_id="")

    with pytest.raises(MissingCredentialsError, match="client id"):
        await client.login()


async def test_login_without_a_saved_login_asks_for_one(make_client):
    client = make_client(refresh_token="", access_token="", token_expiry="")

    with pytest.raises(MissingCredentialsError, match="Not logged in"):
        await client.login()


async def test_a_saved_token_that_still_lasts_is_used_as_it_is(make_client, spotify):
    client = make_client()
    client.logged_in = False

    await client.login()

    assert client.logged_in
    assert spotify.calls == []


async def test_an_expired_token_is_refreshed_and_kept(make_client, spotify):
    spotify.on("POST", "/token", token_answer(refresh_token="ROTATED"))
    client = make_client(token_expiry=str(int(time.time()) - 5))
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
        assert saved.access_token == "NEW"
        assert saved.refresh_token == "ROTATED"
        assert float(saved.token_expiry) > time.time() + 3000
    assert client.global_config.file.modified


async def test_the_old_refresh_token_stays_when_spotify_sends_none(
    make_client, spotify
):
    spotify.on("POST", "/token", token_answer())
    client = make_client(token_expiry="")

    await client.login()

    assert client.config.refresh_token == "REFRESH"
    assert client.global_config.file.spotify.refresh_token == "REFRESH"


async def test_a_refresh_spotify_refuses_asks_for_a_new_login(make_client, spotify):
    spotify.on(
        "POST",
        "/token",
        (400, {"error": "invalid_grant", "error_description": "Refresh token revoked"}),
    )
    client = make_client(token_expiry="")

    with pytest.raises(AuthenticationError, match="Refresh token revoked"):
        await client.login()


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


async def test_requests_carry_the_token(make_client, spotify):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj())
    client = make_client()

    await client.get_metadata(TRACK, "track")

    assert spotify.requests(f"/v1/tracks/{TRACK}")[0]["auth"] == "Bearer TOKEN"


async def test_a_rejected_token_is_replaced_once_and_the_request_repeated(
    make_client, spotify
):
    spotify.on("POST", "/token", token_answer())
    spotify.on(
        "GET",
        f"/v1/tracks/{TRACK}",
        [(401, {"error": {"message": "The access token expired"}}), track_obj()],
    )
    client = make_client()

    track = await client.get_metadata(TRACK, "track")

    assert track["id"] == TRACK
    assert [c["auth"] for c in spotify.requests(f"/v1/tracks/{TRACK}")] == [
        "Bearer TOKEN",
        "Bearer NEW",
    ]


async def test_a_token_that_is_rejected_again_asks_for_a_new_login(
    make_client, spotify
):
    spotify.on("POST", "/token", token_answer())
    spotify.on("GET", f"/v1/tracks/{TRACK}", (401, {"error": {"message": "no"}}))
    client = make_client()

    with pytest.raises(AuthenticationError, match="Log in again"):
        await client.get_metadata(TRACK, "track")

    assert len(spotify.requests("/token")) == 1


async def test_the_premium_rule_is_explained(make_client, spotify):
    message = "Active premium subscription required for the owner of the app."
    spotify.on("GET", f"/v1/tracks/{TRACK}", (403, {"error": {"message": message}}))
    client = make_client()

    with pytest.raises(SpotifyAPIError) as caught:
        await client.get_metadata(TRACK, "track")

    assert str(caught.value) == PREMIUM_REQUIRED
    assert "User Management" in PREMIUM_REQUIRED


async def test_a_missing_item_is_not_found(make_client, spotify):
    client = make_client()

    with pytest.raises(ItemNotFoundError):
        await client.get_metadata(TRACK, "track")


async def test_endless_rate_limiting_is_reported(make_client, spotify):
    spotify.on("GET", f"/v1/tracks/{TRACK}", (429, {"error": {"message": "slow down"}}))
    client = make_client()

    with pytest.raises(APIError, match="too many requests"):
        await client.get_metadata(TRACK, "track")

    assert len(spotify.requests(f"/v1/tracks/{TRACK}")) == 4  # the usual retries


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (TRACK, TRACK),
        (f"spotify:track:{TRACK}", TRACK),
        (f"https://open.spotify.com/track/{TRACK}?si=abc", TRACK),
        (f"https://open.spotify.com/track/{TRACK}/", TRACK),
    ],
)
def test_an_id_is_taken_from_a_uri_or_a_link(given, expected):
    assert plain_id(given) == expected


# --- metadata ----------------------------------------------------------------


async def test_a_track_is_fetched_once_and_knows_its_container(make_client, spotify):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj())
    client = make_client()

    first = await client.get_metadata(TRACK, "track")
    second = await client.get_metadata(f"spotify:track:{TRACK}", "track")

    assert first is second
    assert len(spotify.requests(f"/v1/tracks/{TRACK}")) == 1
    assert first["album"]["container"] == "AAC"


async def test_an_mp3_download_says_so_in_its_container(make_client, spotify):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj())
    client = make_client(audio_format="mp3")

    track = await client.get_metadata(TRACK, "track")

    assert track["album"]["container"] == "MP3"


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


async def test_a_playlist_gives_its_tracks_from_items(make_client, spotify):
    spotify.on("GET", f"/v1/playlists/{PLAYLIST}", {"id": PLAYLIST, "name": "CC Mix"})
    spotify.on(
        "GET",
        f"/v1/playlists/{PLAYLIST}/items",
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

    playlist = await client.get_metadata(PLAYLIST, "playlist")

    assert playlist["name"] == "CC Mix"
    assert [t["id"] for t in playlist["tracks"]] == ["new", "old"]
    assert (
        spotify.requests(f"/v1/playlists/{PLAYLIST}/items")[0]["query"]["limit"] == "50"
    )


async def test_the_tracks_of_a_playlist_need_no_second_request(make_client, spotify):
    spotify.on("GET", f"/v1/playlists/{PLAYLIST}", {"id": PLAYLIST, "name": "Mix"})
    spotify.on(
        "GET",
        f"/v1/playlists/{PLAYLIST}/items",
        {"items": [{"item": track_obj("in-playlist")}], "next": None},
    )
    client = make_client()

    await client.get_metadata(PLAYLIST, "playlist")
    track = await client.get_metadata("in-playlist", "track")

    assert track["album"]["container"] == "AAC"
    assert spotify.requests("/v1/tracks/in-playlist") == []


@pytest.mark.parametrize("status", [403, 404])
async def test_a_playlist_that_is_not_the_users_cannot_be_read(
    make_client, spotify, status
):
    spotify.on(
        "GET",
        f"/v1/playlists/{PLAYLIST}",
        {"id": PLAYLIST, "name": "Somebody else's"},
    )
    spotify.on(
        "GET",
        f"/v1/playlists/{PLAYLIST}/items",
        (status, {"error": {"message": "Forbidden"}}),
    )
    client = make_client()

    with pytest.raises(NonStreamableError) as caught:
        await client.get_metadata(PLAYLIST, "playlist")

    assert str(caught.value) == PLAYLIST_NOT_READABLE


async def test_the_premium_rule_is_not_mistaken_for_a_private_playlist(
    make_client, spotify
):
    spotify.on(
        "GET",
        f"/v1/playlists/{PLAYLIST}",
        (403, {"error": {"message": "premium required"}}),
    )
    client = make_client()

    with pytest.raises(SpotifyAPIError) as caught:
        await client.get_metadata(PLAYLIST, "playlist")

    assert str(caught.value) == PREMIUM_REQUIRED


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


async def test_labels_are_not_a_thing_on_spotify(make_client):
    client = make_client()

    with pytest.raises(NotImplementedError):
        await client.get_metadata("x", "label")


# --- search ------------------------------------------------------------------


def search_answer(media_type, total_pages, per_page):
    """Pages of results by offset; the last has no `next`."""

    def answer(request: web.Request):
        offset = int(request.query["offset"])
        size = int(request.query["limit"])
        pages = {t: (offset // per_page) for t in (media_type,)}
        items = [track_obj(f"id{offset + i}") for i in range(min(size, per_page))]
        last = pages[media_type] + 1 >= total_pages
        return {f"{media_type}s": {"items": items, "next": None if last else "more"}}

    return answer


async def test_a_search_asks_for_ten_at_a_time(make_client, spotify):
    spotify.on("GET", "/v1/search", search_answer("track", total_pages=9, per_page=10))
    client = make_client()

    pages = await client.search("track", "sneaky", limit=25)

    queries = [c["query"] for c in spotify.requests("/v1/search")]
    assert [(q["limit"], q["offset"]) for q in queries] == [
        ("10", "0"),
        ("10", "10"),
        ("5", "20"),
    ]
    assert all(q["q"] == "sneaky" and q["type"] == "track" for q in queries)
    assert sum(len(p["items"]) for p in pages) == 25


async def test_a_search_never_asks_for_more_than_fifty(make_client, spotify):
    spotify.on("GET", "/v1/search", search_answer("track", total_pages=99, per_page=10))
    client = make_client()

    pages = await client.search("track", "sneaky", limit=500)

    assert sum(len(p["items"]) for p in pages) == 50
    assert len(spotify.requests("/v1/search")) == 5


async def test_a_search_stops_when_there_is_nothing_more(make_client, spotify):
    spotify.on("GET", "/v1/search", search_answer("track", total_pages=1, per_page=3))
    client = make_client()

    await client.search("track", "sneaky", limit=50)

    assert len(spotify.requests("/v1/search")) == 1


async def test_search_results_fill_the_menu(make_client, spotify):
    release = ALBUM_SIMPLE | {"album_type": "album", "total_tracks": 12}
    spotify.on("GET", "/v1/search", lambda r: _by_type(r, release))
    client = make_client()

    def results(media_type, pages):
        return SearchResults.from_pages("spotify", media_type, pages).results

    [track] = results("track", await client.search("track", "x", limit=1))
    assert (track.id, track.media_type) == (TRACK, "track")
    assert track.summarize() == "Sneaky Snitch by Kevin MacLeod (2011)"
    assert ("Album", "Sneaky Snitch") in track.details
    assert track.image_url == "https://i.scdn.co/image/large"

    [album] = results("album", await client.search("album", "x", limit=1))
    assert album.summarize() == "Sneaky Snitch by Kevin MacLeod (2011)"
    assert ("Tracks", "12") in album.details
    assert ("Type", "Album") in album.details

    [artist] = results("artist", await client.search("artist", "x", limit=1))
    assert artist.summarize() == "Kevin MacLeod"
    assert artist.image_url == "https://i.scdn.co/image/large"

    [playlist] = results("playlist", await client.search("playlist", "x", limit=1))
    assert playlist.summarize() == "CC Mix by someone"
    assert ("Tracks", "3") in playlist.details
    assert playlist.description == "a mix & more"


def _by_type(request, release):
    media_type = request.query["type"]
    item = {
        "track": track_obj(),
        "album": release,
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
    }[media_type]
    return {f"{media_type}s": {"items": [item, None], "next": None}}


async def test_only_the_four_kinds_can_be_searched(make_client):
    client = make_client()

    with pytest.raises(APIError, match="labels"):
        await client.search("label", "x")


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


async def test_a_track_youtube_music_does_not_have_cannot_be_downloaded(
    make_client, spotify
):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj())
    client = make_client()
    client.matcher = RecordingMatcher(None)

    with pytest.raises(NonStreamableError, match="No match on YouTube Music"):
        await client.get_downloadable(TRACK, 0)


async def test_a_wrong_audio_format_is_reported_when_downloading(make_client, spotify):
    spotify.on("GET", f"/v1/tracks/{TRACK}", track_obj())
    client = make_client(audio_format="flac")
    client.matcher = RecordingMatcher(
        AudioMatch(MatchTrack("Sneaky Snitch", video_id="v"), 1.0)
    )

    with pytest.raises(ValueError, match="m4a"):
        await client.get_downloadable(TRACK, 0)
