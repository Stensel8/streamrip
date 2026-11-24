"""`streamrip web`: downloads, search, the playlist tools and the logins in a browser.

The playlist tests are Music-Sync's, whose web interface this grew out of; the
services and streamrip's downloads are faked.
"""

import asyncio
import io
import logging
import os
import re
import threading
import time

import pytest
from playlist_support import ISRC_A, FakeProvider, FakeServices, track

from streamrip import __version__, progress
from streamrip.config import Config
from streamrip.rip.spotify_login import SpotifyLoginError
from streamrip.sync.auth import Token
from streamrip.sync.csvio import parse_csv
from streamrip.sync.engine import Miss, Step
from streamrip.sync.errors import ApiError
from streamrip.sync.models import Track
from streamrip.web import _secret_key, create_app
from streamrip.web import runner as runner_module
from streamrip.web.jobs import Job, JobObserver
from streamrip.web.runner import Result, Runner

CATALOG = [
    track("Song A", "Artist A", ids={"tidal": "1"}, isrc=ISRC_A),
    track("Song B", "Artist B", ids={"tidal": "2"}),
]
JSON = {"Accept": "application/json"}  # what the page's own fetch() calls send


class FakeRunner(Runner):
    """Downloads nothing: says what it was asked, through a fake run."""

    def __init__(self, config):
        super().__init__(config)
        self.added: list[tuple] = []
        self.logins: list[tuple] = []

    def download(self, job, add):
        added = self.added

        class Main:
            async def add_all(self, urls):
                added.append(("urls", urls))

            async def add_all_by_id(self, items):
                added.append(("items", items))

            async def resolve_csv(self, name, tracks, source, fallback):
                added.append(("csv", name, [t.title for t in tracks], source, fallback))

        asyncio.run(add(Main()))
        return "1 track downloaded"

    def search(self, source, media_type, query):
        self.added.append(("search", source, media_type, query))
        return [
            Result(
                source,
                media_type,
                "42",
                "Meteora",
                "Linkin Park",
                "2003",
                False,
                [("Tracks", "13")],
                "https://img.test/a.jpg",
            ),
            Result(
                source,
                media_type,
                "43",
                "<b>Live</b>",
                None,
                None,
                True,
                [],
                "http://plain.test/a.jpg",
            ),
        ]

    def login_qobuz(self, user_id, token):
        self.logins.append(("qobuz", user_id, token))

    def login_deezer(self, arl):
        self.logins.append(("deezer", arl))

    def login_soundcloud(self, token):
        self.logins.append(("soundcloud", token))


@pytest.fixture
def services(tmp_path) -> FakeServices:
    spotify = FakeProvider(
        "spotify",
        liked=[Track("Song A", ["Artist A"], isrc=ISRC_A), Track("Nope", ["Nobody"])],
    )
    return FakeServices(tmp_path, spotify=spotify, tidal=FakeProvider("tidal", CATALOG))


@pytest.fixture
def runner(services) -> FakeRunner:
    return FakeRunner(services.config)


@pytest.fixture
def client(services, runner):
    return create_app(
        services.config, services=services, runner=runner, secret_key="test-secret"
    ).test_client()


def wait_for(client, job_id: str, timeout: float = 5) -> dict:
    """Poll a background job until it is no longer running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=JSON).get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("the job did not finish")


def started(response) -> str:
    assert response.status_code == 202, response.get_data(as_text=True)
    return response.get_json()["id"]


def csv_tracks(response) -> list[str]:
    return [t.title for t in parse_csv(response.get_data(as_text=True))]


# --- pages and hardening ----------------------------------------------------------------------------


def test_the_server_says_it_is_up(client):
    assert client.get("/health").get_json() == {"status": "ok", "version": __version__}


def test_the_pages_render(client):
    assert "Links" in client.get("/").get_data(as_text=True)
    assert "Transfer" in client.get("/playlists").get_data(as_text=True)
    accounts = client.get("/accounts").get_data(as_text=True)
    assert "Qobuz" in accounts
    assert "Tidal library" in accounts
    assert "http://127.0.0.1:9900/callback" in accounts
    assert "http://127.0.0.1:9900/tidal/callback" in accounts


def test_the_pages_load_nothing_from_other_sites(client):
    for path in ("/", "/playlists", "/accounts"):
        page = client.get(path).get_data(as_text=True)
        assert "/static/style.css" in page, path
        assert "/static/app.js" in page, path
        assert not re.search(r"src=[\"']?https?:|stylesheet\"[^>]*https?:", page), path
        # Links to other sites open apart, and say nothing about where they came from.
        for link in re.findall(r"<a [^>]*href=\"https?:[^>]*>", page):
            assert 'rel="noopener noreferrer"' in link, link
    assert client.get("/static/style.css").mimetype == "text/css"
    assert "javascript" in client.get("/static/app.js").mimetype


def test_the_pages_have_nothing_inline_for_the_policy_to_block(client):
    for path in ("/", "/playlists", "/accounts", "/jobs/nope"):
        page = client.get(path).get_data(as_text=True)
        assert not re.search(
            r"<style|style=|<script(?![^>]*\bsrc=)|\son\w+=|javascript:",
            page,
            re.IGNORECASE,
        ), path


def test_only_our_own_host_names_are_answered(client):
    assert client.get("/", headers={"Host": "localhost:8888"}).status_code == 200
    assert client.get("/", headers={"Host": "127.0.0.1:8888"}).status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_responses_carry_security_headers(client):
    headers = client.get("/").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    policy = headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "unsafe" not in policy
    # Covers come from the services' servers, over https only.
    assert "img-src 'self' https:" in policy


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 302),  # no browser headers: curl or a script
        ({"Sec-Fetch-Site": "same-origin"}, 302),
        ({"Sec-Fetch-Site": "none"}, 302),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        (
            {"Sec-Fetch-Site": "same-site"},
            403,
        ),  # another port on this machine is another origin too
        ({"Origin": "http://localhost"}, 302),  # the test client's own address
        ({"Origin": "https://evil.example"}, 403),
        ({"Origin": "null"}, 403),
    ],
)
def test_changes_started_by_another_site_are_refused(client, headers, status):
    assert (
        client.post("/accounts/spotify/logout", headers=headers).status_code == status
    )


def test_refusals_are_json_for_fetch_calls_and_a_page_for_browsers(client):
    forged = {"Sec-Fetch-Site": "cross-site"}
    assert (
        client.post("/download", headers={**forged, **JSON}).get_json()["status"]
        == "error"
    )
    assert "Error 403" in client.post("/download", headers=forged).get_data(
        as_text=True
    )


def test_the_cookie_key_is_made_once_and_kept_private(tmp_path, monkeypatch):
    monkeypatch.setattr("streamrip.web.APP_DIR", tmp_path)
    key = _secret_key()
    assert len(key) == 64
    assert _secret_key() == key
    if os.name != "nt":
        assert (tmp_path / "web-secret-key").stat().st_mode & 0o777 == 0o600
    (tmp_path / "web-secret-key").write_text(
        "", encoding="utf-8"
    )  # a crash before it was filled
    assert len(_secret_key()) == 64  # an empty key would make every login fail
    monkeypatch.setattr("streamrip.web.APP_DIR", tmp_path / "missing" / "folder")
    assert len(_secret_key()) == 64  # no folder to write to: one for this run


# --- downloading ----------------------------------------------------------------------------------------


def test_links_are_downloaded_as_a_job(client, runner):
    urls = "https://tidal.com/browse/album/234793\nhttps://tidal.com/browse/album/234793 https://www.deezer.com/album/1"
    job = wait_for(
        client, started(client.post("/download", json={"urls": urls}, headers=JSON))
    )
    assert job["status"] == "done"
    assert job["message"] == "1 track downloaded"
    assert job["title"] == "Downloading 2 links"
    assert job["phases"] == ["find", "download"]
    # A link given twice is downloaded once.
    assert runner.added == [
        (
            "urls",
            ["https://tidal.com/browse/album/234793", "https://www.deezer.com/album/1"],
        )
    ]


def test_a_reloaded_page_finds_the_job_that_is_running(client, services):
    jobs = client.application.extensions["jobs"]
    assert client.get("/jobs/running").get_json() == {"id": None}
    release = threading.Event()
    job = jobs.start("download", "Downloading", lambda job: release.wait(5) and "ok")
    assert client.get("/jobs/running").get_json() == {"id": job.id}
    release.set()
    wait_for(client, job.id)
    assert client.get("/jobs/running").get_json() == {"id": None}


@pytest.mark.parametrize(
    ("urls", "message"),
    [("", "Paste one or more links"), ("https://example.com/x", "Not a link")],
)
def test_a_link_streamrip_does_not_know_is_refused_at_once(client, urls, message):
    response = client.post("/download", json={"urls": urls}, headers=JSON)
    assert response.status_code == 400
    assert message in response.get_json()["message"]


def test_search_results_are_downloaded_by_id(client, runner):
    found = client.post(
        "/search",
        json={"source": "qobuz", "media_type": "album", "query": "meteora"},
        headers=JSON,
    ).get_json()
    assert [r["name"] for r in found] == [
        "Meteora",
        "<b>Live</b>",
    ]  # escaped by the page, not here
    # Only covers over https: the policy would block the others anyway.
    assert [r["image"] for r in found] == ["https://img.test/a.jpg", None]
    items = [{k: found[0][k] for k in ("source", "media_type", "id")}]
    job = wait_for(
        client, started(client.post("/download", json={"items": items}, headers=JSON))
    )
    assert job["status"] == "done"
    assert runner.added[-1] == ("items", [("qobuz", "album", "42")])


@pytest.mark.parametrize(
    "body",
    [
        {"source": "napster", "media_type": "album", "query": "x"},
        {"source": "qobuz", "media_type": "song", "query": "x"},
        {"source": "qobuz", "media_type": "album", "query": "  "},
    ],
)
def test_a_search_needs_a_source_kind_and_words(client, body):
    assert client.post("/search", json=body, headers=JSON).status_code == 400


@pytest.mark.parametrize(
    "items", [[{"source": "x", "media_type": "album", "id": 1}], [{"id": 1}], ["text"]]
)
def test_items_to_download_are_checked(client, items):
    assert (
        client.post("/download", json={"items": items}, headers=JSON).status_code == 400
    )


def test_a_csv_list_is_searched_on_the_chosen_source(client, runner):
    response = client.post(
        "/download/csv",
        data={
            "file": (io.BytesIO(b"Artist A,Song A\n"), "mix.csv"),
            "source": "qobuz",
            "fallback": "deezer",
        },
        headers=JSON,
        content_type="multipart/form-data",
    )
    job = wait_for(client, started(response))
    assert job["title"] == "Downloading mix from Qobuz"
    assert runner.added == [("csv", "mix", ["Song A"], "qobuz", "deezer")]


def test_an_uploaded_file_is_named_after_its_name_alone(client, runner):
    """A browser may send the whole path of the file, of the folders too."""
    response = client.post(
        "/download/csv",
        data={
            "file": (
                io.BytesIO(b"Artist A,Song A\n"),
                "old/browsers/../send/paths/mix.csv",
            ),
            "source": "qobuz",
        },
        headers=JSON,
        content_type="multipart/form-data",
    )
    wait_for(client, started(response))
    assert runner.added[0][1] == "mix"


def test_a_playlist_is_read_and_downloaded_from_another_source(client, runner):
    response = client.post(
        "/playlists/spotify/download", json={"source": "tidal"}, headers=JSON
    )
    job = wait_for(client, started(response))
    assert job["status"] == "done"
    assert job["phases"] == ["read", "find", "download"]
    assert runner.added == [("csv", "Liked Songs", ["Song A", "Nope"], "tidal", None)]


def test_a_download_shows_its_tracks_and_log(services, monkeypatch):
    """The real runner, with a fake run: what the page gets to see."""

    class FakeMain:
        def __init__(self, config, *, interactive):
            assert not interactive  # never a question in the server's terminal
            self.media = []
            self.database = type(
                "D", (), {"downloaded_now": 2, "skipped_now": 1, "failed_now": 0}
            )()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def resolve(self):
            self.media = ["album"]

        async def rip(self):
            progress.overall_add(3, enabled=False)
            with progress.get_progress_callback(
                False, 100, "Artist - Track 1"
            ) as update:
                update(40)
                seen.append(job.to_json())
            progress.overall_advance(1, enabled=False)
            logging.getLogger("streamrip").warning("Track 2 is not streamable")

    seen = []
    monkeypatch.setattr(runner_module, "Main", FakeMain)
    job = Job("id", "download", "Downloading 1 link")

    async def add(_main):
        pass

    summary = Runner(services.config).download(job, add)
    assert summary.startswith("2 tracks downloaded, 1 already downloaded, in ")
    (during,) = seen
    assert during["rows"] == [{"desc": "Artist - Track 1", "done": 40, "total": 100}]
    assert (during["phase"], during["done"], during["total"]) == ("download", 0, 3)
    assert job.to_json()["log"] == [
        {"level": "warning", "text": "Track 2 is not streamable"}
    ]
    assert progress._observer is None  # the page stops following once it is over


def test_nothing_new_to_download_is_no_failure(services, monkeypatch):
    """Everything already downloaded: the run is over, not broken."""

    class FakeMain:
        def __init__(self, config, *, interactive):
            self.media = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def resolve(self):
            pass

    async def add(_main):
        pass

    monkeypatch.setattr(runner_module, "Main", FakeMain)
    job = Job("id", "download", "x")
    assert (
        Runner(services.config).download(job, add)
        == "Nothing new to download; the log says why."
    )


def test_a_refused_login_leaves_the_one_before(services, monkeypatch):
    class RefusedError(Exception):
        pass

    class Refusing:
        def __init__(self, config):
            pass

        async def login(self):
            raise RefusedError("Qobuz did not accept the token")

        async def close(self):
            pass

    monkeypatch.setattr(runner_module, "QobuzClient", Refusing)
    qobuz = services.config.session.qobuz
    qobuz.user_id, qobuz.auth_token = "old-id", "old-token"
    with pytest.raises(RefusedError):
        Runner(services.config).login_qobuz("new-id", "new-token")
    assert (qobuz.user_id, qobuz.auth_token) == ("old-id", "old-token")
    assert (
        services.config.session.qobuz is qobuz
    )  # the same section a download may hold


def test_the_observer_follows_titles_and_ignores_lookups():
    job = Job("id", "download", "x")
    observer = JobObserver(job)
    observer.overall_add(2)
    observer.title(1, "Meteora")
    assert job.text == "Downloading Meteora"
    with observer.row("resolve", "Fetching track info", 10) as update:
        update(5)
    assert job.rows == {}
    observer.title(1, None)
    assert job.titles == {}


# --- logging in -----------------------------------------------------------------------------------------


def test_qobuz_and_deezer_logins_are_tried_and_kept(client, runner):
    response = client.post(
        "/accounts/qobuz/login", data={"user_id": "1", "auth_token": "t"}
    )
    assert response.status_code == 302
    assert (
        client.post("/accounts/deezer/login", data={"arl": "secret"}).status_code == 302
    )
    assert runner.logins == [("qobuz", "1", "t"), ("deezer", "secret")]
    assert "Logged in to Deezer" in client.get("/accounts").get_data(as_text=True)
    client.post("/accounts/qobuz/login", data={"user_id": "1"})
    assert "Fill in both" in client.get("/accounts").get_data(as_text=True)


def test_an_app_is_saved_in_the_config(client, services):
    client.post(
        "/accounts/tidal-library/app", data={"client_id": "tid", "client_secret": "sec"}
    )
    client.post("/accounts/spotify/app", data={"client_id": "sid"})
    saved = Config(services.config.path).file
    assert (
        saved.tidal.api_client_id,
        saved.tidal.api_client_secret,
        saved.spotify.client_id,
    ) == ("tid", "sec", "sid")
    # A saved secret need not be typed again to change the id.
    client.post("/accounts/tidal-library/app", data={"client_id": "tid2"})
    assert Config(services.config.path).file.tidal.api_client_secret == "sec"


def test_a_browser_login_takes_a_pasted_address(client, services, monkeypatch):
    """The browser is on another device: the redirect cannot arrive, the address is pasted."""

    def wait_for_redirect(_uri, _timeout, stop):
        stop.wait(5)  # until the address is pasted
        raise SpotifyLoginError("Timed out waiting for the login to finish.")

    monkeypatch.setattr("streamrip.web.logins._wait_for_redirect", wait_for_redirect)
    job_id = started(client.post("/accounts/spotify/login", headers=JSON))
    deadline = time.monotonic() + 5
    while not (job := client.get(f"/jobs/{job_id}", headers=JSON).get_json())["link"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert job["pasteable"]
    assert job["link"].startswith("https://auth.test/authorize?state=")
    state = job["link"].split("state=")[1].split("&")[0]
    response = client.post(
        f"/jobs/{job_id}/paste",
        json={"address": "http://127.0.0.1:9900/callback?code=C&state=bad"},
        headers=JSON,
    )
    assert response.status_code == 400
    assert "state mismatch" in response.get_json()["message"]
    address = f"http://127.0.0.1:9900/callback?code=CODE&state={state}"
    assert (
        client.post(
            f"/jobs/{job_id}/paste", json={"address": address}, headers=JSON
        ).status_code
        == 200
    )
    job = wait_for(client, job_id)
    assert job["status"] == "done"
    assert job["message"] == "Logged in to Spotify."
    assert services.fake_oauth.exchanged[0] == "CODE"
    assert Config(services.config.path).file.spotify.access_token == "ACCESS"


def test_a_waiting_login_can_be_cancelled_and_ends_when_the_app_changes(
    client, monkeypatch
):
    def wait_for_redirect(_uri, _timeout, stop):
        stop.wait(5)
        raise SpotifyLoginError("The login was cancelled.")

    monkeypatch.setattr("streamrip.web.logins._wait_for_redirect", wait_for_redirect)
    for end in (
        lambda job_id: client.post(f"/jobs/{job_id}/cancel", headers=JSON),
        lambda _: client.post(
            "/accounts/tidal-library/app", data={"client_id": "x", "client_secret": "y"}
        ),
    ):
        job_id = started(client.post("/accounts/tidal-library/login", headers=JSON))
        time.sleep(0.1)  # let the login start waiting
        end(job_id)
        job = wait_for(client, job_id)
        assert (job["status"], job["message"]) == ("error", "The login was cancelled.")


def test_logging_out_removes_the_login(client, services):
    services.store.save("tidal", Token("A", "R", time.time() + 100))
    assert client.post("/accounts/tidal-library/logout").status_code == 302
    assert services.store.load("tidal") is None


# --- the playlist tools (Music-Sync's tests) ----------------------------------------------------------


def export(client, service: str, playlist: str | None = None):
    return client.post(
        f"/playlists/{service}/export", json={"playlist": playlist}, headers=JSON
    )


def transfer(client, **fields):
    return client.post(
        "/playlists/transfer",
        json={"source": "spotify", "target": "tidal", **fields},
        headers=JSON,
    )


def upload(client, text: bytes, playlist: str = "Mix", name: str = "in.csv"):
    return client.post(
        "/playlists/tidal/import",
        data={"file": (io.BytesIO(text), name), "playlist": playlist},
        headers=JSON,
        content_type="multipart/form-data",
    )


def test_export_runs_as_a_job_and_then_downloads_the_csv(client, services):
    job_id = started(export(client, "spotify"))
    job = wait_for(client, job_id)
    assert job["status"] == "done"
    assert job["message"] == "Liked Songs: 2 tracks exported."
    assert job["download"]
    assert job["phases"] == ["read"]
    assert job["title"] == "Export from Spotify"
    download = client.get(f"/jobs/{job_id}/download")
    assert download.mimetype == "text/csv"
    assert "attachment" in download.headers["Content-Disposition"]
    assert csv_tracks(download) == ["Song A", "Nope"]

    services.providers["tidal"].existing_playlist("Röad trip", track("In list"))
    job_id = started(export(client, "tidal", "Röad trip"))
    assert wait_for(client, job_id)["status"] == "done"
    disposition = client.get(f"/jobs/{job_id}/download").headers["Content-Disposition"]
    # The name with its accent for browsers that read filename*, an ASCII one for the rest.
    assert "filename*=UTF-8''R%C3%B6ad%20trip.csv" in disposition
    assert 'filename="Rad trip.csv"' in disposition

    job = wait_for(client, started(export(client, "tidal", "missing")))
    assert job["status"] == "error"
    assert "No playlist" in job["message"]


def test_the_playlists_of_a_service_are_listed(client, services):
    services.providers["tidal"].existing_playlist("<i>Mix</i>", track("In list"))
    assert client.get("/playlists/tidal/list").get_json() == [
        {"id": "pl1", "name": "<i>Mix</i>", "track_count": 1, "readable": True}
    ]


def test_only_an_export_has_a_download(client):
    job_id = started(transfer(client))
    wait_for(client, job_id)
    assert client.get(f"/jobs/{job_id}/download").status_code == 404


def test_import_runs_as_a_job_and_reports_what_was_not_found(client, services):
    job_id = started(upload(client, b"Artist A,Song A\nNobody,Nothing\n"))
    job = wait_for(client, job_id)
    assert job["status"] == "done"
    assert job["message"] == "Mix: 1 matched, 1 not found; added 1"
    assert job["unmatched"] == [{"track": "Nobody - Nothing", "why": "not on Tidal"}]
    assert (
        job["found"],
        job["not_found"],
        job["phase"],
        job["done"],
        job["total"],
    ) == (1, 1, "add", 1, 1)
    tidal = services.providers["tidal"]
    assert [tidal.native_id(t) for t in tidal.playlists_by_id["pl1"][1]] == ["1"]
    assert csv_tracks(client.get(f"/jobs/{job_id}/unmatched.csv")) == ["Nothing"]


def test_import_can_add_albums(client, services):
    tidal = services.providers["tidal"]
    tidal.albums = [track("Discovery", "Daft Punk", ids={"tidal": "5"})]
    response = client.post(
        "/playlists/tidal/import",
        data={
            "file": (io.BytesIO(b"Daft Punk,Discovery\n"), "albums.csv"),
            "contains": "albums",
        },
        headers=JSON,
        content_type="multipart/form-data",
    )
    job = wait_for(client, started(response))
    assert job["message"] == "Favourite albums: 1 matched, 0 not found; added 1"
    assert [a.ids for a in tidal.favorite_albums] == [{"tidal": "5"}]


@pytest.mark.parametrize(
    ("body", "message"), [(b"", "no tracks"), ("Björk,Jóga".encode("latin-1"), "UTF-8")]
)
def test_import_rejects_empty_and_non_utf8_files(client, body, message):
    response = upload(client, body)
    assert response.status_code == 400
    assert message in response.get_json()["message"]


def test_import_needs_a_file(client):
    assert (
        client.post("/playlists/tidal/import", data={}, headers=JSON).status_code == 400
    )


def test_transfer_copies_liked_songs_and_can_go_to_the_favourites(client, services):
    job = wait_for(client, started(transfer(client)))
    assert job["status"] == "done"
    assert "Liked Songs (from Spotify): 1 matched, 1 not found" in job["message"]
    assert job["title"] == "Transfer from Spotify to Tidal"
    assert job["phases"] == ["read", "check", "match", "add"]
    tidal = services.providers["tidal"]
    assert [name for name, _ in tidal.playlists_by_id.values()] == [
        "Liked Songs (from Spotify)"
    ]

    job = wait_for(client, started(transfer(client, into="favorites")))
    assert job["status"] == "done"
    assert [tidal.native_id(t) for t in tidal.liked] == ["1"]


@pytest.mark.parametrize(
    "body",
    [
        {"source": "tidal", "target": "tidal"},
        {"source": "x", "target": "tidal"},
        {},
        [],
        "text",
        {"source": ["spotify"]},
    ],
)
def test_transfer_validates_its_input(client, body):
    response = client.post("/playlists/transfer", json=body, headers=JSON)
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


def test_a_failing_service_is_reported_and_a_bug_does_not_leak(
    client, services, tmp_path
):
    spotify = services.providers["spotify"]

    def explode():
        raise ApiError(500, "The service fell over")

    spotify.liked_tracks = explode
    job = wait_for(client, started(transfer(client)))
    assert job["status"] == "error"
    assert "fell over" in job["message"]

    def crash():
        raise RuntimeError("secret internal detail")

    spotify.liked_tracks = crash
    job = wait_for(client, started(transfer(client)))
    assert job["status"] == "error"
    assert "secret internal detail" not in job["message"]


def test_a_running_job_says_what_it_is_doing():
    job = Job("id", "transfer", "Transfer from Spotify to Tidal")
    job.progress(Step("read", "Reading Liked Songs from Spotify", 10, None))
    assert (job.phase, job.done, job.total) == ("read", 10, None)
    for done, title in enumerate("AB", start=1):
        found = Track(title, ["X"])
        job.progress(
            Step(
                "match",
                "Finding the tracks on Tidal",
                done,
                3,
                found,
                0,
                Miss(found, "not on Tidal"),
            )
        )
    shown = job.to_json()
    assert (shown["phase"], shown["done"], shown["total"]) == ("match", 2, 3)
    assert (shown["found"], shown["not_found"], shown["current"]) == (0, 2, "X - B")


def test_unknown_jobs_are_a_404(client):
    assert client.get("/jobs/nope", headers=JSON).status_code == 404
    assert client.get("/jobs/nope/unmatched.csv").status_code == 404
    assert client.get("/jobs/nope/download").status_code == 404
    assert client.post("/jobs/nope/paste", json={}, headers=JSON).status_code == 404


# --- the accounts page is where every login is ----------------------------------------------------


def test_every_login_is_on_the_accounts_page_and_nowhere_else(client):
    accounts = client.get("/accounts").get_data(as_text=True)
    for login in ("qobuz", "tidal", "deezer", "soundcloud", "spotify", "tidal-library"):
        assert f'id="{login}"' in accounts
    for path in ("/", "/playlists"):
        page = client.get(path).get_data(as_text=True)
        assert "Accounts page" not in page, path
        assert "Log in" not in page, path


def test_soundcloud_takes_a_token_and_a_guest_is_no_failure(client, runner):
    assert "Guest" in client.get("/accounts").get_data(as_text=True)
    assert (
        client.post(
            "/accounts/soundcloud/login", data={"oauth_token": "2-1"}
        ).status_code
        == 302
    )
    assert runner.logins == [("soundcloud", "2-1")]
    client.post("/accounts/soundcloud/login", data={})
    assert "Paste the oauth_token" in client.get("/accounts").get_data(as_text=True)


def test_every_login_can_be_logged_out_from_the_page(client, services):
    services.config.update_logins("deezer", {"arl": "cookie"})
    assert client.post("/accounts/deezer/logout").status_code == 302
    assert services.config.session.deezer.arl == ""
    assert "Logged out of Deezer" in client.get("/accounts").get_data(as_text=True)
    assert "Not logged in to Deezer" in (
        client.post("/accounts/deezer/logout", follow_redirects=True).get_data(
            as_text=True
        )
    )


def test_checking_all_logins_is_a_job_that_reports_each(client, services, monkeypatch):
    import streamrip.web.runner as runner_module
    from streamrip.verify import Result

    async def fake(_config, ids=None):
        return [
            Result("qobuz", "ok", "Studio, hi-res"),
            Result("deezer", "failed", "expired", "streamrip login deezer"),
        ]

    monkeypatch.setattr(runner_module, "verify_logins", fake)
    job = wait_for(client, started(client.post("/accounts/verify", headers=JSON)))
    assert job["status"] == "done"
    assert job["message"] == "1 of 2 are fine; log in again to Deezer."
    assert [(line["level"], line["text"]) for line in job["log"]] == [
        ("info", "Qobuz: ok, Studio, hi-res"),
        ("error", "Deezer: failed, expired"),
    ]
