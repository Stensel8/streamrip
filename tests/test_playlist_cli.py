"""`streamrip playlist`: Music-Sync's commands, as part of streamrip."""

import csv

import pytest
from click.testing import CliRunner
from playlist_support import ISRC_A, FakeProvider, FakeServices, config_at, track

from streamrip.config import Config
from streamrip.exceptions import FFmpegNotFoundError
from streamrip.rip import accounts_cli, playlist_cli
from streamrip.rip.cli import rip
from streamrip.sync.auth import Token
from streamrip.sync.csvio import read_tracks, write_tracks
from streamrip.sync.models import Track
from streamrip.sync.providers.tidal import TidalOAuth
from streamrip.sync.services import Services

CATALOG = [
    track("Song A", "Artist A", ids={"tidal": "1"}, isrc=ISRC_A),
    track("Song B", "Artist B", ids={"tidal": "2"}),
]


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    """Run `streamrip *args` on a config in tmp_path, with `services` in place of
    the real ones: (exit code, stdout, stderr)."""

    def invoke(*args, services=None):
        if services is not None:
            services.config.session.metadata.musicbrainz = False
            monkeypatch.setattr(playlist_cli, "_services", lambda _ctx: services)
            monkeypatch.setattr(accounts_cli, "Services", lambda _config: services)
        config = tmp_path / "config.toml"
        if not config.exists():
            config_at(tmp_path)
        result = CliRunner().invoke(rip, ["--config-path", str(config), *args])
        captured = capsys.readouterr()
        stderr = result.stderr + captured.err
        return result.exit_code, result.stdout + captured.out, stderr

    return invoke


def liked_songs(tmp_path, *liked):
    return FakeServices(tmp_path, spotify=FakeProvider("spotify", liked=liked))


def test_export_liked_songs_to_csv(tmp_path, cli):
    liked = track(
        "Song A", "Artist A", ids={"spotify": "spotify:track:1"}, isrc=ISRC_A, album="A"
    )
    out = tmp_path / "liked.csv"
    code, stdout, _ = cli(
        *("playlist", "export", "spotify", "--liked", "-o", str(out)),
        services=liked_songs(tmp_path, liked),
    )
    assert code == 0
    assert read_tracks(out) == [liked]
    assert "1 tracks" in stdout


def test_export_all_keeps_playlists_whose_file_names_would_collide(tmp_path, cli):
    spotify = FakeProvider("spotify", liked=[track("Liked one")])
    for name, title in [
        ("Chill", "First"),
        ("Chill", "Second"),  # Spotify lets two playlists share a name
        ("Road/trip", "Third"),
        ("Road:trip", "Fourth"),  # a file name drops what told these apart
        ("Empty", None),
    ]:
        spotify.existing_playlist(name, *([track(title)] if title else []))

    folder = tmp_path / "all"
    code, _, stderr = cli(
        *("playlist", "export", "spotify", "--all", "-q", "-o", str(folder)),
        services=FakeServices(tmp_path, spotify=spotify),
    )
    assert code == 0
    written = {p.name: [t.title for t in read_tracks(p)] for p in folder.iterdir()}
    assert written == {
        "Liked Songs.csv": ["Liked one"],
        "Chill.csv": ["First"],
        "Chill (2).csv": ["Second"],
        "Road_trip.csv": ["Third"],
        "Road_trip (2).csv": ["Fourth"],
    }
    assert "skipping 'Empty': it is empty" in stderr


def test_a_missing_or_empty_playlist_is_an_error_not_a_traceback(tmp_path, cli):
    tidal = FakeProvider("tidal")
    tidal.existing_playlist("Empty")
    services = FakeServices(tmp_path, tidal=tidal)
    out = tmp_path / "x.csv"
    for name, error in [("missing", "No playlist"), ("Empty", "is empty")]:
        args = ("playlist", "export", "tidal", "--playlist", name, "-o", str(out))
        code, _, stderr = cli(*args, services=services)
        assert code == 1
        assert error in stderr
    assert not out.exists()


def test_import_a_file_without_a_header_row(tmp_path, cli):
    csv_file = tmp_path / "in.csv"
    csv_file.write_text("Artist A,Song A\nNobody,Nothing\n", encoding="utf-8")
    tidal = FakeProvider("tidal", CATALOG)
    unmatched = tmp_path / "unmatched.csv"
    code, stdout, _ = cli(
        *("playlist", "import", "tidal", str(csv_file), "--playlist", "Mix", "-q"),
        *("--unmatched", str(unmatched)),
        services=FakeServices(tmp_path, tidal=tidal),
    )
    assert code == 0
    assert [tidal.native_id(t) for t in tidal.playlists_by_id["pl1"][1]] == ["1"]
    assert "Mix: 1 matched, 1 not found; added 1" in stdout
    assert [t.title for t in read_tracks(unmatched)] == ["Nothing"]


def test_import_dry_run_and_an_empty_file(tmp_path, cli):
    csv_file = tmp_path / "in.csv"
    write_tracks(csv_file, [Track("Song A", ["Artist A"], isrc=ISRC_A)])
    tidal = FakeProvider("tidal", CATALOG)
    services = FakeServices(tmp_path, tidal=tidal)
    code, stdout, _ = cli(
        *("playlist", "import", "tidal", str(csv_file), "--dry-run", "-q"),
        services=services,
    )
    assert code == 0
    assert tidal.playlists_by_id == {}
    assert "would add 1" in stdout

    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    code, _, stderr = cli("playlist", "import", "tidal", str(empty), services=services)
    assert code == 1
    assert "No tracks found" in stderr


def test_import_albums_like_csv2tidal(tmp_path, cli):
    csv_file = tmp_path / "albums.csv"
    csv_file.write_text("Daft Punk,Discovery\n", encoding="utf-8")  # artist,album
    tidal = FakeProvider("tidal")
    tidal.albums = [track("Discovery", "Daft Punk", ids={"tidal": "5"})]
    code, stdout, _ = cli(
        *("playlist", "import", "tidal", str(csv_file), "--albums", "-q"),
        services=FakeServices(tmp_path, tidal=tidal),
    )
    assert code == 0
    assert [a.ids for a in tidal.favorite_albums] == [{"tidal": "5"}]
    assert "Favourite albums: 1 matched, 0 not found; added 1" in stdout


def test_a_transfer_creates_a_playlist_and_reports_every_track(tmp_path, cli):
    liked = [
        Track("Song A", ["Artist A"], isrc=ISRC_A),
        Track("Song B (Live)", ["Artist B"], duration_ms=200_000),
    ]
    tidal = FakeProvider("tidal", CATALOG)
    services = FakeServices(
        tmp_path, spotify=FakeProvider("spotify", liked=liked), tidal=tidal
    )
    report = tmp_path / "audit.csv"
    code, stdout, _ = cli(
        *(
            "playlist",
            "transfer",
            "spotify",
            "tidal",
            "--liked",
            "--report",
            str(report),
        ),
        services=services,
    )
    assert code == 0
    assert [name for name, _ in tidal.playlists_by_id.values()] == [
        "Liked Songs (from Spotify)"
    ]
    assert "Artist B - Song B (Live): only another version; closest:" in stdout
    rows = {r["wanted title"]: r for r in csv.DictReader(report.open(encoding="utf-8"))}
    matched, missed = rows["Song A"], rows["Song B (Live)"]
    assert (matched["result"], matched["how"]) == ("matched", "isrc")
    assert missed["result"] == "not found"
    assert missed["found title"] == "Song B"  # the closest it came


def test_a_transfer_to_the_same_service_is_refused(tmp_path, cli):
    services = liked_songs(tmp_path, track("Song A"))
    code, _, stderr = cli(
        *("playlist", "transfer", "spotify", "spotify", "--liked"), services=services
    )
    assert code == 2
    assert "the same service" in stderr


def test_a_transfer_keeps_playlists_that_share_a_name_apart(tmp_path, cli):
    spotify = FakeProvider("spotify")
    spotify.existing_playlist("Chill", track("Song A", "Artist A", isrc=ISRC_A))
    spotify.existing_playlist("chill", track("Song B", "Artist B"))
    tidal = FakeProvider("tidal", CATALOG)
    code, _, _ = cli(
        *("playlist", "transfer", "spotify", "tidal", "--all", "-q"),
        services=FakeServices(tmp_path, spotify=spotify, tidal=tidal),
    )
    assert code == 0
    assert {
        name: [t.title for t in ts] for name, ts in tidal.playlists_by_id.values()
    } == {
        "Chill": ["Song A"],
        "chill (2)": ["Song B"],
    }


def test_liked_songs_go_to_the_favourites(tmp_path, cli):
    tidal = FakeProvider("tidal", CATALOG)
    spotify = FakeProvider(
        "spotify", liked=[Track("Song A", ["Artist A"], isrc=ISRC_A)]
    )
    services = FakeServices(tmp_path, spotify=spotify, tidal=tidal)
    code, stdout, _ = cli(
        *("playlist", "transfer", "spotify", "tidal", "--sync-favorites", "-q"),
        services=services,
    )
    assert code == 0
    assert tidal.playlists_by_id == {}
    assert [tidal.native_id(t) for t in tidal.liked] == ["1"]
    assert "Liked Songs: 1 matched" in stdout

    csv_file = tmp_path / "in.csv"
    write_tracks(csv_file, [Track("Song B", ["Artist B"])])
    code, _, _ = cli(
        *("playlist", "import", "tidal", str(csv_file), "--to-favorites", "-q"),
        services=services,
    )
    assert code == 0
    assert [tidal.native_id(t) for t in tidal.liked] == ["1", "2"]


def test_without_an_app_it_says_how_to_make_one(cli):
    code, _, stderr = cli("playlist", "list", "tidal")
    assert code == 1
    assert "developer.tidal.com" in stderr
    assert "streamrip login tidal-library" in stderr


def test_accounts_and_logout(tmp_path, cli):
    code, stdout, _ = cli("accounts")
    assert code == 0
    assert "needs an app first" in stdout
    assert "not logged in" in stdout

    tidal = {"api_client_id": "c", "api_client_secret": "s"}
    tidal |= {"api_access_token": "a", "api_refresh_token": "r"}
    config_at(tmp_path, tidal=tidal)
    assert "tidal-library  logged in" in cli("accounts")[1]
    code, stdout, _ = cli("logout", "tidal-library")
    assert code == 0
    assert "Logged out of Tidal library" in stdout
    saved = Config(tmp_path / "config.toml").file.tidal
    assert (saved.api_access_token, saved.api_client_id) == ("", "c")  # the app stays


def test_the_tidal_api_login_asks_for_the_app_and_keeps_the_login(
    tmp_path, cli, monkeypatch
):
    services = FakeServices(tmp_path)
    services.config.session.tidal.api_client_id = ""
    answers = iter(["my-client-id", "my-secret"])
    monkeypatch.setattr(accounts_cli.Prompt, "ask", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(accounts_cli, "launch", lambda _url: None)
    monkeypatch.setattr(accounts_cli, "_browser_code", lambda url, *_: "CODE")
    monkeypatch.setattr(FakeServices, "oauth", Services.oauth)  # a Tidal login
    token = Token("ACCESS", "REFRESH", 4_000_000_000)
    monkeypatch.setattr(TidalOAuth, "exchange", lambda self, code, verifier: token)

    code, stdout, _ = cli("login", "tidal-library", services=services)
    assert code == 0
    assert "Logged in to Tidal library" in stdout
    saved = Config(tmp_path / "config.toml").file.tidal
    assert (saved.api_client_id, saved.api_client_secret) == (
        "my-client-id",
        "my-secret",
    )
    assert (saved.api_access_token, saved.api_refresh_token) == ("ACCESS", "REFRESH")


def fake_session(main=None, error=None):
    """A stand-in for the downloads' session: gives `main`, or raises `error`."""

    class Session:
        def __init__(self, _ctx):
            pass

        async def __aenter__(self):
            if error:
                raise error
            return main

        async def __aexit__(self, *_):
            pass

    return Session


def test_download_hands_each_list_to_the_downloads(tmp_path, cli, monkeypatch):
    spotify = FakeProvider("spotify", liked=[track("Song A", "Artist A")])
    spotify.existing_playlist("Road trip", track("Song B", "Artist B"))
    asked = []

    class Main:
        async def resolve_csv(self, name, tracks, source, fallback):
            asked.append((name, [t.title for t in tracks], source, fallback))

        async def rip(self):
            asked.append("rip")

    monkeypatch.setattr(playlist_cli, "main_session", fake_session(Main()))
    code, _, _ = cli(
        *("playlist", "download", "spotify", "--all", "-q", "--source", "qobuz"),
        *("-fs", "deezer"),
        services=FakeServices(tmp_path, spotify=spotify),
    )
    assert code == 0
    assert asked == [
        ("Liked Songs", ["Song A"], "qobuz", "deezer"),
        ("Road trip", ["Song B"], "qobuz", "deezer"),
        "rip",
    ]


def test_a_download_without_ffmpeg_is_a_message_not_a_traceback(
    tmp_path, cli, monkeypatch
):
    error = FFmpegNotFoundError("No ffmpeg installation found")
    monkeypatch.setattr(playlist_cli, "main_session", fake_session(error=error))
    code, stdout, stderr = cli(
        *("playlist", "download", "spotify", "--liked", "-q", "--source", "qobuz"),
        services=liked_songs(tmp_path, track("Song A")),
    )
    assert code == 1
    assert "No ffmpeg installation found" in stdout + stderr
    assert "Traceback" not in stdout + stderr
