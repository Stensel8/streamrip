"""`streamrip playlist`: Music-Sync's commands, as part of streamrip."""

import csv

import pytest
from click.testing import CliRunner
from playlist_support import ISRC_A, FakeProvider, FakeServices, config_at, track

from streamrip.config import Config
from streamrip.rip import accounts_cli, playlist_cli
from streamrip.rip.cli import rip
from streamrip.sync.auth import Token
from streamrip.sync.csvio import read_tracks, write_tracks
from streamrip.sync.models import Track
from streamrip.sync.services import Services

CATALOG = [
    track("Song A", "Artist A", ids={"tidal": "1"}, isrc=ISRC_A),
    track("Song B", "Artist B", ids={"tidal": "2"}),
]


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    monkeypatch.setenv("STREAMRIP_NO_UPDATE_CHECK", "1")


def run(tmp_path, monkeypatch, capsys, args, services: Services | None = None):
    """`streamrip playlist *args` with `services`; (exit code, stdout, stderr)."""
    if services is not None:
        monkeypatch.setattr(playlist_cli, "_services", lambda _ctx: services)
    config = tmp_path / "config.toml"
    if not config.exists():
        config_at(tmp_path)
    result = CliRunner().invoke(rip, ["--config-path", str(config), "playlist", *args])
    captured = capsys.readouterr()
    return result.exit_code, result.stdout + captured.out, result.stderr + captured.err


def run_root(tmp_path, monkeypatch, capsys, args, services: Services | None = None):
    """`streamrip *args` (accounts, login, logout); (exit code, stdout, stderr)."""
    if services is not None:
        monkeypatch.setattr(accounts_cli, "Services", lambda _config: services)
    config = tmp_path / "config.toml"
    if not config.exists():
        config_at(tmp_path)
    result = CliRunner().invoke(rip, ["--config-path", str(config), *args])
    captured = capsys.readouterr()
    return result.exit_code, result.stdout + captured.out, result.stderr + captured.err


def test_export_liked_songs_to_csv(tmp_path, monkeypatch, capsys):
    liked = [
        track(
            "Song A",
            "Artist A",
            ids={"spotify": "spotify:track:1"},
            isrc=ISRC_A,
            album="Album",
        )
    ]
    services = FakeServices(tmp_path, spotify=FakeProvider("spotify", liked=liked))
    out = tmp_path / "liked.csv"
    code, stdout, _ = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["export", "spotify", "--liked", "-o", str(out)],
        services,
    )
    assert code == 0
    assert read_tracks(out) == liked
    assert "1 tracks" in stdout


def test_export_all_keeps_playlists_whose_file_names_would_collide(
    tmp_path, monkeypatch, capsys
):
    # Spotify lets two playlists share a name, and a file name drops characters that told two names apart.
    spotify = FakeProvider("spotify", liked=[track("Liked one")])
    for name, title in [
        ("Chill", "First"),
        ("Chill", "Second"),
        ("chill", "Third"),
        ("Liked Songs", "Fourth"),
    ]:
        spotify.existing_playlist(name, track(title))
    spotify.existing_playlist("Road/trip", track("Fifth"))
    spotify.existing_playlist("Road:trip", track("Sixth"))
    spotify.existing_playlist("Empty")

    folder = tmp_path / "all"
    args = ["export", "spotify", "--all", "-q", "-o", str(folder)]
    code, _, stderr = run(
        tmp_path, monkeypatch, capsys, args, FakeServices(tmp_path, spotify=spotify)
    )
    assert code == 0
    written = {
        path.name: [t.title for t in read_tracks(path)] for path in folder.iterdir()
    }
    assert written == {
        "Liked Songs.csv": ["Liked one"],
        "Chill.csv": ["First"],
        "Chill (2).csv": ["Second"],
        "chill (3).csv": ["Third"],
        "Liked Songs (2).csv": ["Fourth"],
        "Road_trip.csv": ["Fifth"],
        "Road_trip (2).csv": ["Sixth"],
    }
    assert "skipping 'Empty': it is empty" in stderr


def test_one_of_liked_playlist_and_all_is_asked_for(tmp_path, monkeypatch, capsys):
    services = FakeServices(tmp_path, spotify=FakeProvider("spotify"))
    for args in (["export", "spotify"], ["export", "spotify", "--liked", "--all"]):
        code, _, stderr = run(tmp_path, monkeypatch, capsys, args, services)
        assert code == 2
        assert "Choose one of --liked" in stderr


def test_a_missing_or_empty_playlist_is_an_error_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    tidal = FakeProvider("tidal")
    tidal.existing_playlist("Empty")
    services = FakeServices(tmp_path, tidal=tidal)
    out = tmp_path / "x.csv"
    code, _, stderr = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["export", "tidal", "--playlist", "missing", "-o", str(out)],
        services,
    )
    assert code == 1
    assert "No playlist with name or id 'missing'" in stderr
    code, _, stderr = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["export", "tidal", "--playlist", "Empty", "-o", str(out)],
        services,
    )
    assert code == 1
    assert '"Empty" is empty' in stderr
    assert not out.exists()


def test_import_a_file_without_a_header_row(tmp_path, monkeypatch, capsys):
    csv_file = tmp_path / "in.csv"
    csv_file.write_text("Artist A,Song A\nNobody,Nothing\n", encoding="utf-8")
    tidal = FakeProvider("tidal", CATALOG)
    unmatched = tmp_path / "unmatched.csv"
    args = [
        "import",
        "tidal",
        str(csv_file),
        "--playlist",
        "Mix",
        "-q",
        "--unmatched",
        str(unmatched),
    ]
    code, stdout, _ = run(
        tmp_path, monkeypatch, capsys, args, FakeServices(tmp_path, tidal=tidal)
    )
    assert code == 0
    assert [tidal.native_id(t) for t in tidal.playlists_by_id["pl1"][1]] == ["1"]
    assert "Mix: 1 matched, 1 not found; added 1" in stdout
    assert [t.title for t in read_tracks(unmatched)] == ["Nothing"]


def test_import_dry_run_and_an_empty_file(tmp_path, monkeypatch, capsys):
    csv_file = tmp_path / "in.csv"
    write_tracks(csv_file, [Track("Song A", ["Artist A"], isrc=ISRC_A)])
    tidal = FakeProvider("tidal", CATALOG)
    services = FakeServices(tmp_path, tidal=tidal)
    code, stdout, _ = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["import", "tidal", str(csv_file), "--dry-run", "-q"],
        services,
    )
    assert code == 0
    assert tidal.playlists_by_id == {}
    assert "would add 1" in stdout

    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    code, _, stderr = run(
        tmp_path, monkeypatch, capsys, ["import", "tidal", str(empty)], services
    )
    assert code == 1
    assert "No tracks found" in stderr


def test_a_transfer_shows_each_step_and_what_came_closest(
    tmp_path, monkeypatch, capsys
):
    liked = [
        Track("Song A", ["Artist A"], isrc=ISRC_A),
        Track("Song B (Live)", ["Artist B"], duration_ms=200_000),
    ]
    tidal = FakeProvider("tidal", CATALOG)
    services = FakeServices(
        tmp_path, spotify=FakeProvider("spotify", liked=liked), tidal=tidal
    )
    code, stdout, stderr = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["transfer", "spotify", "tidal", "--liked"],
        services,
    )
    assert code == 0
    assert [name for name, _ in tidal.playlists_by_id.values()] == [
        "Liked Songs (from Spotify)"
    ]
    # Not a terminal, so one line per completed step instead of a line redrawn in place.
    assert stderr.splitlines() == [
        "Reading Liked Songs from Spotify  [####################]  2/2",
        "Finding the tracks on Tidal  [####################]  2/2  1 found, 1 not found",
        "Adding to Liked Songs (from Spotify) on Tidal  [####################]  1/1",
    ]
    assert (
        "Artist B - Song B (Live): only another version; closest: Artist B - Song B"
        in stdout
    )

    code, _, stderr = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["transfer", "spotify", "spotify", "--liked"],
        services,
    )
    assert code == 2
    assert "the same service" in stderr


def test_a_transfer_can_report_every_track_with_what_it_was_matched_to(
    tmp_path, monkeypatch, capsys
):
    liked = [
        Track("Song A", ["Artist A"], isrc=ISRC_A),
        Track("Song B (Live)", ["Artist B"], duration_ms=200_000),
    ]
    services = FakeServices(
        tmp_path,
        spotify=FakeProvider("spotify", liked=liked),
        tidal=FakeProvider("tidal", CATALOG),
    )
    out = tmp_path / "audit.csv"
    code, stdout, _ = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["transfer", "spotify", "tidal", "--liked", "--report", str(out)],
        services,
    )
    assert code == 0
    assert "2 tracks reported" in stdout
    rows = {r["wanted title"]: r for r in csv.DictReader(out.open(encoding="utf-8"))}
    matched, missed = rows["Song A"], rows["Song B (Live)"]
    assert (matched["result"], matched["how"], matched["found title"]) == (
        "matched",
        "isrc",
        "Song A",
    )
    assert missed["result"] == "not found"
    assert missed["why"].startswith("only another version")
    assert missed["found title"] == "Song B"  # the closest it came


def test_sync_favorites_puts_liked_songs_in_the_favourites(
    tmp_path, monkeypatch, capsys
):
    tidal = FakeProvider("tidal", CATALOG)
    spotify = FakeProvider(
        "spotify", liked=[Track("Song A", ["Artist A"], isrc=ISRC_A)]
    )
    services = FakeServices(tmp_path, spotify=spotify, tidal=tidal)
    code, stdout, _ = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["transfer", "spotify", "tidal", "--sync-favorites", "-q"],
        services,
    )
    assert code == 0
    assert tidal.playlists_by_id == {}
    assert [tidal.native_id(t) for t in tidal.liked] == ["1"]
    assert "Liked Songs: 1 matched" in stdout

    csv_file = tmp_path / "in.csv"
    write_tracks(csv_file, [Track("Song B", ["Artist B"])])
    code, _, _ = run(
        tmp_path,
        monkeypatch,
        capsys,
        ["import", "tidal", str(csv_file), "--to-favorites", "-q"],
        services,
    )
    assert code == 0
    assert [tidal.native_id(t) for t in tidal.liked] == ["1", "2"]
    args = ["transfer", "spotify", "tidal", "--sync-favorites", "--to-playlist", "X"]
    assert run(tmp_path, monkeypatch, capsys, args, services)[0] == 2


def test_import_albums_like_csv2tidal(tmp_path, monkeypatch, capsys):
    csv_file = tmp_path / "albums.csv"
    csv_file.write_text(
        "Daft Punk,Discovery\n", encoding="utf-8"
    )  # csv2tidal's format: artist,album
    tidal = FakeProvider("tidal")
    tidal.albums = [track("Discovery", "Daft Punk", ids={"tidal": "5"})]
    args = ["import", "tidal", str(csv_file), "--albums", "-q"]
    code, stdout, _ = run(
        tmp_path, monkeypatch, capsys, args, FakeServices(tmp_path, tidal=tidal)
    )
    assert code == 0
    assert [a.ids for a in tidal.favorite_albums] == [{"tidal": "5"}]
    assert "Favourite albums: 1 matched, 0 not found; added 1" in stdout


def test_without_an_app_it_says_how_to_make_one(tmp_path, monkeypatch, capsys):
    code, _, stderr = run(tmp_path, monkeypatch, capsys, ["list", "tidal"])
    assert code == 1
    assert "developer.tidal.com" in stderr
    assert "streamrip login tidal-library" in stderr


def test_accounts_and_logout(tmp_path, monkeypatch, capsys):
    code, stdout, _ = run_root(tmp_path, monkeypatch, capsys, ["accounts"])
    assert code == 0
    assert "needs an app first" in stdout
    assert "not logged in" in stdout
    assert "soundcloud" in stdout

    config_at(
        tmp_path,
        tidal={
            "api_client_id": "c",
            "api_client_secret": "s",
            "api_access_token": "a",
            "api_refresh_token": "r",
        },
    )
    code, stdout, _ = run_root(tmp_path, monkeypatch, capsys, ["accounts"])
    assert "tidal-library  logged in" in stdout
    code, stdout, _ = run_root(
        tmp_path, monkeypatch, capsys, ["logout", "tidal-library"]
    )
    assert code == 0
    assert "Logged out of Tidal library" in stdout
    saved = Config(tmp_path / "config.toml").file.tidal
    assert (saved.api_access_token, saved.api_client_id) == ("", "c")  # the app stays


def test_download_hands_each_list_to_the_downloads(tmp_path, monkeypatch, capsys):
    """A Spotify playlist, searched and downloaded on Qobuz like a CSV list."""
    spotify = FakeProvider("spotify", liked=[track("Song A", "Artist A", isrc=ISRC_A)])
    spotify.existing_playlist("Road trip", track("Song B", "Artist B"))
    asked = []

    class FakeMain:
        async def resolve_csv(self, name, tracks, source, fallback):
            asked.append((name, [t.title for t in tracks], source, fallback))

        async def rip(self):
            asked.append("rip")

    class Session:
        def __init__(self, _ctx):
            pass

        async def __aenter__(self):
            return FakeMain()

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(playlist_cli, "main_session", Session)
    args = ["download", "spotify", "--all", "-q", "--source", "qobuz", "-fs", "deezer"]
    code, _, _ = run(
        tmp_path, monkeypatch, capsys, args, FakeServices(tmp_path, spotify=spotify)
    )
    assert code == 0
    assert asked == [
        ("Liked Songs", ["Song A"], "qobuz", "deezer"),
        ("Road trip", ["Song B"], "qobuz", "deezer"),
        "rip",
    ]


def test_the_tidal_api_login_asks_for_the_app_and_keeps_the_login(
    tmp_path, monkeypatch, capsys
):
    services = FakeServices(tmp_path)
    services.config.session.tidal.api_client_id = ""
    answers = iter(["my-client-id", "my-secret"])
    monkeypatch.setattr(accounts_cli.Prompt, "ask", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(accounts_cli, "launch", lambda _url: None)
    monkeypatch.setattr(accounts_cli, "_browser_code", lambda url, *_: "CODE")

    def exchange(_code, _verifier):
        return Token("ACCESS", "REFRESH", 4_000_000_000)

    real_oauth = Services.oauth

    def oauth(self, service):
        client = real_oauth(self, service)
        client.exchange = exchange
        return client

    monkeypatch.setattr(FakeServices, "oauth", oauth)
    code, stdout, _ = run_root(
        tmp_path, monkeypatch, capsys, ["login", "tidal-library"], services
    )
    assert code == 0
    assert "Logged in to Tidal library" in stdout
    saved = Config(tmp_path / "config.toml").file.tidal
    assert (saved.api_client_id, saved.api_client_secret) == (
        "my-client-id",
        "my-secret",
    )
    assert (saved.api_access_token, saved.api_refresh_token) == ("ACCESS", "REFRESH")


def test_a_download_without_ffmpeg_is_a_message_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """As every download command: through `coro`, which explains it."""
    from streamrip.exceptions import FFmpegNotFoundError

    class NoFFmpeg:
        def __init__(self, _ctx):
            pass

        async def __aenter__(self):
            raise FFmpegNotFoundError("No ffmpeg installation found")

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(playlist_cli, "main_session", NoFFmpeg)
    spotify = FakeProvider("spotify", liked=[track("Song A")])
    args = ["download", "spotify", "--liked", "-q", "--source", "qobuz"]
    code, stdout, stderr = run(
        tmp_path, monkeypatch, capsys, args, FakeServices(tmp_path, spotify=spotify)
    )
    assert code == 1
    assert "No ffmpeg installation found" in stdout + stderr
    assert "Traceback" not in stdout + stderr
