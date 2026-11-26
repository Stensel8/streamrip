"""`streamrip web`: downloads, search, the playlist tools and the logins in a browser.

The services and streamrip's downloads are faked.
"""

import asyncio
import io
import logging
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from playlist_support import ISRC_A, FakeProvider, FakeServices, track

from streamrip import __version__, db, progress
from streamrip.client import spotify as spotify_module
from streamrip.config import Config
from streamrip.exceptions import AccountHalt, AuthenticationError, SearchTypeError
from streamrip.rip import repair as repair_module
from streamrip.rip.spotify_login import SpotifyLoginError
from streamrip.sync.auth import Token
from streamrip.sync.csvio import parse_csv
from streamrip.sync.errors import ApiError
from streamrip.sync.models import Track
from streamrip.verify import Result as Verified
from streamrip.web import _secret_key, create_app
from streamrip.web import runner as runner_module
from streamrip.web.jobs import Job, JobManager
from streamrip.web.runner import Result, Runner

CATALOG = [
    track("Song A", "Artist A", ids={"tidal": "1"}, isrc=ISRC_A),
    track("Song B", "Artist B", ids={"tidal": "2"}),
]
JSON = {"Accept": "application/json"}  # what the page's own fetch() calls send
TRANSFER = {"source": "spotify", "target": "tidal"}


class FakeRunner(Runner):
    """Downloads nothing: says what it was asked."""

    def __init__(self, config):
        super().__init__(config)
        self.added: list[tuple] = []
        self.logins: list[tuple] = []

    def download(self, job, add):
        added = self.added

        class Main:
            config = SimpleNamespace(session=SimpleNamespace(lastfm=SimpleNamespace()))

            async def add_all(self, urls):
                added.append(("urls", urls))

            async def add_all_by_id(self, items):
                added.append(("items", items))

            async def add_by_id(self, source, media_type, item_id):
                added.append(("id", source, media_type, item_id))

            async def resolve_csv(self, name, tracks, source, fallback):
                added.append(("csv", name, [t.title for t in tracks], source, fallback))

            async def resolve_lastfm(self, url):
                lastfm = self.config.session.lastfm
                added.append(("lastfm", url, lastfm.source, lastfm.fallback_source))

        asyncio.run(add(Main()))
        return "1 track downloaded"

    def plan(self, job, add):
        self.download(job, add)
        return "Nothing was downloaded."

    def repair(self, job, flat=False):
        self.added.append(("repair", flat))
        return "Repaired 1 of 1 download."

    def search(self, source, media_type, query):
        return [
            Result(source, media_type, "42", "Meteora", "Linkin Park", "2003", False,
                   [("Tracks", "13")], "https://img.test/a.jpg"),
            Result(source, media_type, "43", "<b>Live</b>", None, None, True,
                   [], "http://plain.test/a.jpg"),
        ]  # fmt: skip

    def login_qobuz(self, user_id, token):
        self.logins.append(("qobuz", user_id, token))

    def login_deezer(self, arl):
        self.logins.append(("deezer", arl))

    def login_soundcloud(self, token):
        self.logins.append(("soundcloud", token))


class FakeMain:
    """streamrip's Main with nothing to download, for the real Runner."""

    def __init__(self, config, *, interactive):
        assert not interactive  # nobody is at the keyboard of a server
        self.media = []
        self.database = SimpleNamespace(downloaded_now=2, skipped_now=1, failed_now=0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def resolve(self):
        pass

    async def rip(self):
        pass


async def nothing(_main):
    pass


@pytest.fixture
def services(tmp_path) -> FakeServices:
    spotify = FakeProvider(
        "spotify",
        liked=[Track("Song A", ["Artist A"], isrc=ISRC_A), Track("Nope", ["Nobody"])],
    )
    services = FakeServices(
        tmp_path, spotify=spotify, tidal=FakeProvider("tidal", CATALOG)
    )
    services.config.session.metadata.musicbrainz = False  # no lookups on the internet
    return services


@pytest.fixture
def runner(services) -> FakeRunner:
    return FakeRunner(services.config)


@pytest.fixture
def client(services, runner):
    return create_app(
        services.config, services=services, runner=runner, secret_key="test-secret"
    ).test_client()


def wait_for(client, job_id: str, ready=lambda job: job["status"] != "running") -> dict:
    """Poll a background job until `ready`."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=JSON).get_json()
        if ready(job):
            return job
        time.sleep(0.02)
    raise AssertionError("the job did not get there")


def started(response) -> str:
    assert response.status_code == 202, response.get_data(as_text=True)
    return response.get_json()["id"]


def run(client, path, **kwargs) -> dict:
    """POST to a route that starts a job, wait for it, and give the job."""
    job_id = started(client.post(path, headers=JSON, **kwargs))
    return {**wait_for(client, job_id), "id": job_id}


def csv_upload(text: bytes, name: str = "in.csv", **fields) -> dict:
    return {
        "data": {"file": (io.BytesIO(text), name), **fields},
        "content_type": "multipart/form-data",
    }


def csv_tracks(response) -> list[str]:
    return [t.title for t in parse_csv(response.get_data(as_text=True))]


def test_the_pages_render(client):
    assert client.get("/health").get_json() == {"status": "ok", "version": __version__}
    for path in (
        "/",
        "/playlists",
        "/accounts",
        "/library",
        "/settings",
        "/settings/file",
    ):
        assert '<div id="app">' in client.get(path).get_data(as_text=True), path
    assert client.get("/download/api").get_json()["sources"]
    accounts = client.get("/accounts/api").get_json()
    assert accounts["accounts"]["qobuz"]["label"] == "Qobuz"
    assert accounts["redirect_uris"]["spotify"] == "http://127.0.0.1:9900/callback"


def test_the_pages_need_nothing_the_security_policy_blocks(client):
    assert "unsafe" not in client.get("/").headers["Content-Security-Policy"]
    for path in ("/", "/playlists", "/accounts"):
        page = client.get(path).get_data(as_text=True)
        assert not re.search(
            r"<style|style=|<script(?![^>]*\bsrc=)|\son\w+=|javascript:|src=[\"']?https?:",
            page,
            re.IGNORECASE,
        ), path


def test_only_our_own_host_names_are_answered(client):
    assert client.get("/", headers={"Host": "localhost:8888"}).status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 200),  # no browser headers: curl or a script
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Sec-Fetch-Site": "same-site"}, 403),  # another port on this machine
        ({"Origin": "https://evil.example"}, 403),
    ],
)
def test_changes_started_by_another_site_are_refused(client, headers, status):
    response = client.post("/accounts/spotify/logout", headers=headers)
    assert response.status_code == status


def test_the_cookie_key_is_made_once_and_kept_private(tmp_path, monkeypatch):
    monkeypatch.setattr("streamrip.web.APP_DIR", tmp_path)
    key = _secret_key()
    assert len(key) == 64
    assert _secret_key() == key
    if os.name != "nt":
        assert (tmp_path / "web-secret-key").stat().st_mode & 0o777 == 0o600
    (tmp_path / "web-secret-key").write_text("")  # a crash before it was filled
    assert len(_secret_key()) == 64


def test_links_are_downloaded_as_a_job(client, runner):
    urls = "https://tidal.com/browse/album/1\nhttps://tidal.com/browse/album/1 https://www.deezer.com/album/2"
    job = run(client, "/download", json={"urls": urls})
    assert (job["status"], job["message"]) == ("done", "1 track downloaded")
    assert job["title"] == "Downloading 2 links"
    # A link given twice is downloaded once.
    assert runner.added == [
        ("urls", ["https://tidal.com/browse/album/1", "https://www.deezer.com/album/2"])
    ]


@pytest.mark.parametrize(
    ("urls", "message"),
    [("", "Paste one or more links"), ("https://example.com/x", "Not a link")],
)
def test_a_link_streamrip_does_not_know_is_refused_at_once(client, urls, message):
    response = client.post("/download", json={"urls": urls}, headers=JSON)
    assert response.status_code == 400
    assert message in response.get_json()["message"]


def test_a_reloaded_page_finds_the_job_that_is_running(client):
    jobs = client.application.extensions["jobs"]
    assert client.get("/jobs/running").get_json() == {"id": None}
    release = threading.Event()
    login = jobs.start("login", "Log in", lambda job: release.wait(5) and "ok")
    assert client.get("/jobs/running").get_json() == {"id": login.id}
    # Each page only picks up the kinds of job it shows.
    assert client.get("/jobs/running?kinds=login verify").get_json() == {"id": login.id}
    assert client.get("/jobs/running?kinds=download plan").get_json() == {"id": None}
    release.set()
    wait_for(client, login.id)
    assert client.get("/jobs/running").get_json() == {"id": None}


def test_search_results_are_downloaded_by_id(client, runner):
    body = {"source": "qobuz", "media_type": "album", "query": "meteora"}
    found = client.post("/search", json=body, headers=JSON).get_json()
    assert [r["name"] for r in found] == ["Meteora", "<b>Live</b>"]
    assert [r["image"] for r in found] == ["https://img.test/a.jpg", None]  # https only
    items = [{k: found[0][k] for k in ("source", "media_type", "id")}]
    assert run(client, "/download", json={"items": items})["status"] == "done"
    assert runner.added[-1] == ("items", [("qobuz", "album", "42")])


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AccountHalt("Paused to protect your account"), 429),
        (SearchTypeError("Soundcloud has no search for albums"), 400),
    ],
)
def test_a_search_that_cannot_go_on_is_explained_and_not_a_dropped_connection(
    client, runner, monkeypatch, error, status
):
    async def refused(*_):
        raise error

    # FakeRunner fakes the search; this one goes through the real `call`.
    monkeypatch.setattr(runner, "search", lambda *args: Runner.search(runner, *args))
    monkeypatch.setattr(runner, "_search", refused)
    body = {"source": "soundcloud", "media_type": "album", "query": "x"}
    response = client.post("/search", json=body, headers=JSON)
    assert response.status_code == status
    assert str(error) in response.get_json()["message"]


def test_the_search_offers_a_source_only_the_kinds_it_has(client):
    sources = client.get("/download/api").get_json()["sources"]
    kinds = {s["name"]: s["kinds"] for s in sources}
    assert kinds["soundcloud"] == ["track", "playlist"]
    assert kinds["qobuz"] == ["track", "album", "artist", "playlist"]


def test_an_item_is_downloaded_by_its_id(client, runner):
    body = {"source": "qobuz", "media_type": "album", "id": "234793"}
    assert run(client, "/download/id", json=body)["status"] == "done"
    assert runner.added == [("id", "qobuz", "album", "234793")]


def test_links_can_come_from_a_file_and_be_previewed(client, runner):
    upload = csv_upload(b"https://www.deezer.com/album/2\n", "links.txt")
    upload["data"] |= {"urls": "https://tidal.com/browse/album/1", "plan": "1"}
    job = run(client, "/download", **upload)
    assert job["title"].startswith("Preview: ")
    assert job["message"] == "Nothing was downloaded."
    assert runner.added == [
        ("urls", ["https://tidal.com/browse/album/1", "https://www.deezer.com/album/2"])
    ]


def test_a_csv_list_is_searched_on_the_chosen_source(client, runner):
    upload = csv_upload(
        b"Artist A,Song A\n", "mix.csv", source="qobuz", fallback="deezer"
    )
    job = run(client, "/download/csv", **upload)
    assert job["title"] == "Downloading mix from Qobuz"
    assert runner.added == [("csv", "mix", ["Song A"], "qobuz", "deezer")]


def test_a_lastfm_playlist_is_searched_with_a_chain_of_fallbacks(client, runner):
    url = "https://www.last.fm/user/x/playlists/1"
    body = {"url": url, "source": "tidal", "fallback": "qobuz", "then": "deezer"}
    run(client, "/download/lastfm", json=body)
    assert runner.added == [("lastfm", url, "tidal", "qobuz,deezer")]
    wrong = client.post(
        "/download/lastfm", json={**body, "url": "https://example.com/x"}, headers=JSON
    )
    assert wrong.status_code == 400


def test_a_playlist_is_read_and_downloaded_from_another_source(client, runner):
    job = run(client, "/playlists/spotify/download", json={"source": "tidal"})
    assert job["status"] == "done"
    assert runner.added == [("csv", "Liked Songs", ["Song A", "Nope"], "tidal", None)]
    # Its preview is not the Download page's, which a reload there picks up.
    preview = run(
        client, "/playlists/spotify/download", json={"source": "tidal", "plan": "1"}
    )
    assert (preview["kind"], preview["phases"]) == ("playlist-plan", ["read", "find"])


def test_the_oldest_jobs_that_are_over_are_forgotten_never_one_that_runs():
    jobs = JobManager(keep=2)
    release = threading.Event()
    long = jobs.start("download", "Long", lambda job: release.wait(5) and "ok")
    quick = []
    for _ in range(3):
        quick.append(job := jobs.start("plan", "Quick", lambda job: "ok"))
        while job.status == "running":
            time.sleep(0.01)
    assert [jobs.get(j.id) for j in (long, *quick)] == [long, None, None, quick[-1]]
    release.set()


def test_the_library_page_counts_and_forgets(client, services, tmp_path):
    database = services.config.session.database
    database.downloads_path = str(tmp_path / "downloads.db")
    database.failed_downloads_path = str(tmp_path / "failed.db")
    downloads = db.Downloads(database.downloads_path)
    downloads.add((db.download_key("qobuz", "1"), ""))
    assert client.get("/library/api").get_json()["downloaded"] == 1
    cleared = client.post("/library/clear/downloads").get_json()
    assert cleared["message"] == "Forgot 1 downloaded track."
    assert len(downloads.all()) == 0


def test_failed_downloads_are_retried_as_a_job(client, runner):
    job = run(client, "/library/repair", json={"flat": True})
    assert job["message"] == "Repaired 1 of 1 download."
    assert runner.added == [("repair", True)]


def test_a_finished_transfer_counts_what_it_found_once_each():
    from streamrip.web.jobs import Job

    job = Job("id", "transfer", "Transfer from Spotify to Tidal")
    job.found = 61  # the running count: 58 tracks, and 3 that the playlist lists twice
    job.matched = [object()] * 58

    assert job.to_json()["found"] == 61  # still running: the count so far
    job.status = "done"
    assert job.to_json()["found"] == 58


def test_a_download_says_how_long_it_has_to_go_from_the_first_track():
    # Three tracks had to be done before there was any estimate, and an album
    # of long tracks takes ten minutes for them.
    from streamrip.web.jobs import Job, Row

    job = Job("id", "download", "Downloading an album")
    job.phase, job.total, job.done = "download", 14, 2
    job.phase_started = time.monotonic() - 300
    job.rows[0] = Row("Band - Track 3", 100, done=50, started=time.monotonic() - 30)

    shown = job.to_json()

    assert shown["rows"][0]["eta"] == pytest.approx(30, abs=1)  # half done in 30 s
    assert shown["eta"] == pytest.approx(
        300 / 2.5 * 11.5, rel=0.01
    )  # 2 and a half done


def test_a_download_shows_its_tracks_and_log(services, monkeypatch):
    qobuz = services.config.session.qobuz
    qobuz.quality = 4
    job = Job("id", "download", "Downloading 1 link")
    seen = []

    class Main(FakeMain):
        async def resolve(self):
            self.media = ["album"]
            qobuz.quality = 2  # as a login does for the run

        async def rip(self):
            progress.overall_add(3, enabled=False)
            with progress.get_progress_callback(False, 100, "Artist - Track 1") as bar:
                bar(40)
                seen.append(job.to_json())
            progress.overall_advance(1, enabled=False)
            logging.getLogger("streamrip").warning("Track 2 is not streamable")

    monkeypatch.setattr(runner_module, "Main", Main)
    summary = Runner(services.config).download(job, nothing)
    assert summary.startswith("2 tracks downloaded, 1 already downloaded, in ")
    (during,) = seen
    assert [during["rows"][0][k] for k in ("desc", "done", "total")] == [
        "Artist - Track 1",
        40,
        100,
    ]
    assert (during["phase"], during["done"], during["total"]) == ("download", 0, 3)
    assert job.to_json()["log"] == [
        {"level": "warning", "text": "Track 2 is not streamable"}
    ]
    assert qobuz.quality == 4  # a server does not keep what a login lowered
    assert progress._observer is None  # the page stops following once it is over


def test_nothing_new_to_download_is_no_failure(services, monkeypatch):
    monkeypatch.setattr(runner_module, "Main", FakeMain)
    summary = Runner(services.config).download(Job("id", "download", "x"), nothing)
    assert summary == "Nothing new to download; the log says why."


def test_a_download_preview_says_what_it_would_do(services, monkeypatch):
    monkeypatch.setattr(runner_module, "Main", FakeMain)
    lines = (["Album: 9 tracks"], ["Qobuz: 9 tracks, at full speed"])
    monkeypatch.setattr(runner_module, "plan_lines", lambda _main: lines)
    job = Job("p", "plan", "Preview")
    assert Runner(services.config).plan(job, nothing) == "Nothing was downloaded."
    assert [text for _, text in job.log] == [*lines[0], *lines[1]]


def test_a_repair_says_how_many_it_repaired(services, tmp_path, monkeypatch):
    database = services.config.session.database
    database.failed_downloads_path = str(tmp_path / "failed.db")
    failed = db.Failed(database.failed_downloads_path)
    failed.add(("qobuz", "track", "1"))
    failed.add(("qobuz", "track", "2"))

    async def repaired(config, flat=False, *, interactive=True):
        assert (flat, interactive) == (True, False)
        return 1

    monkeypatch.setattr(runner_module, "repair_downloads", repaired)
    message = Runner(services.config).repair(Job("r", "repair", "Retry"), flat=True)
    assert message == "Repaired 1 of 2 downloads; 1 failed again and stay in the list."


async def test_a_repair_puts_back_the_settings_a_server_goes_on_with(
    services, monkeypatch
):
    seen = {}

    async def fake(config, interactive):
        seen["singles"] = config.session.filepaths.add_singles_to_folder
        seen["recording"] = config.session.database.downloads_enabled
        return 0

    monkeypatch.setattr(repair_module, "_repair", fake)
    session = services.config.session
    session.filepaths.add_singles_to_folder = False
    session.database.downloads_enabled = False
    await repair_module.repair_downloads(services.config)
    assert seen == {"singles": True, "recording": True}  # what a repair needs
    assert session.filepaths.add_singles_to_folder is False
    assert session.database.downloads_enabled is False


def test_a_refused_login_leaves_the_one_before(services, monkeypatch):
    class Refusing:
        def __init__(self, config):
            pass

        async def login(self):
            raise RuntimeError("Qobuz did not accept the token")

        async def close(self):
            pass

    monkeypatch.setattr(runner_module, "QobuzClient", Refusing)
    qobuz = services.config.session.qobuz
    qobuz.user_id, qobuz.auth_token = "old-id", "old-token"
    with pytest.raises(RuntimeError):
        Runner(services.config).login_qobuz("new-id", "new-token")
    assert (qobuz.user_id, qobuz.auth_token) == ("old-id", "old-token")


def test_logins_are_tried_and_kept(client, runner):
    qobuz = {"user_id": "1", "auth_token": "t"}
    assert client.post("/accounts/qobuz/login", json=qobuz).status_code == 200
    assert client.post("/accounts/deezer/login", json={"arl": "x"}).status_code == 200
    soundcloud = {"oauth_token": "2-1"}
    assert client.post("/accounts/soundcloud/login", json=soundcloud).status_code == 200
    assert runner.logins == [
        ("qobuz", "1", "t"),
        ("deezer", "x"),
        ("soundcloud", "2-1"),
    ]
    no_token = client.post("/accounts/qobuz/login", json={"user_id": "1"}, headers=JSON)
    assert "Fill in both" in no_token.get_json()["message"]
    assert len(runner.logins) == 3


def test_an_app_is_saved_in_the_config(client, services):
    app = {"client_id": "tid", "client_secret": "sec"}
    client.post("/accounts/tidal-library/app", data=app)
    client.post("/accounts/spotify/app", data={"client_id": "sid"})
    saved = Config(services.config.path).file
    assert (saved.tidal.api_client_id, saved.tidal.api_client_secret) == ("tid", "sec")
    assert saved.spotify.client_id == "sid"
    # A saved secret need not be typed again to change the id.
    client.post("/accounts/tidal-library/app", data={"client_id": "tid2"})
    assert Config(services.config.path).file.tidal.api_client_secret == "sec"


def test_logging_out_removes_the_login(client, services):
    services.store.save("tidal", Token("A", "R", time.time() + 100))
    services.config.update_logins("deezer", {"arl": "cookie"})
    assert client.post("/accounts/tidal-library/logout").status_code == 200
    assert client.post("/accounts/deezer/logout").status_code == 200
    assert services.store.load("tidal") is None
    assert services.config.session.deezer.arl == ""


def test_a_browser_login_takes_a_pasted_address(client, services, monkeypatch):
    # The browser may be on another device: the redirect cannot arrive.
    def wait_for_redirect(_uri, _timeout, stop):
        stop.wait(5)
        raise SpotifyLoginError("Timed out waiting for the login to finish.")

    monkeypatch.setattr("streamrip.web.logins._wait_for_redirect", wait_for_redirect)
    job_id = started(client.post("/accounts/spotify/login", headers=JSON))
    job = wait_for(client, job_id, lambda job: job["link"])
    assert job["pasteable"]
    state = job["link"].split("state=")[1].split("&")[0]

    paste = f"/jobs/{job_id}/paste"
    wrong = "http://127.0.0.1:9900/callback?code=C&state=bad"
    response = client.post(paste, json={"address": wrong}, headers=JSON)
    assert response.status_code == 400
    assert "state mismatch" in response.get_json()["message"]
    right = f"http://127.0.0.1:9900/callback?code=CODE&state={state}"
    assert client.post(paste, json={"address": right}, headers=JSON).status_code == 200

    job = wait_for(client, job_id)
    assert (job["status"], job["message"]) == ("done", "Logged in to Spotify.")
    spotify = Config(services.config.path).file.spotify
    assert spotify.access_token == "ACCESS"
    assert services.fake_oauth.exchanged[0] == "CODE"


def test_spotifys_own_audio_is_paired_on_the_accounts_page(
    client, services, monkeypatch
):
    async def paired(self, api_key):
        return "sten"

    monkeypatch.setattr(spotify_module.SpotifyClient, "pair_soloist", paired)
    job_id = started(
        client.post("/accounts/soloist/pair", json={"api_key": " KEY "}, headers=JSON)
    )
    job = wait_for(client, job_id)
    assert job["message"] == "Signed in to Spotify's own audio as sten."
    assert Config(services.config.path).file.spotify.soloist_api_key == "KEY"


def test_a_pairing_that_fails_keeps_no_key(client, services, monkeypatch):
    async def refused(self, api_key):
        raise AuthenticationError("Spotify did not accept the pairing")

    monkeypatch.setattr(spotify_module.SpotifyClient, "pair_soloist", refused)
    assert (
        client.post("/accounts/soloist/pair", json={}, headers=JSON).status_code == 400
    )
    job_id = started(
        client.post("/accounts/soloist/pair", json={"api_key": "KEY"}, headers=JSON)
    )
    assert wait_for(client, job_id)["status"] == "error"
    assert not Config(services.config.path).file.spotify.soloist_api_key


def test_the_playlist_page_offers_the_services_that_are_logged_in(client, services):
    login = {"client_id": "id", "access_token": "a", "refresh_token": "r"}
    services.config.update_logins("spotify", login)

    playlist_services = client.get("/playlists/api").get_json()["services"]

    # The services to move playlists between, not the sources; Tidal is not logged in.
    assert playlist_services == [{"name": "spotify", "label": "Spotify"}]


def test_the_accounts_page_says_what_each_login_is_and_gives(client, monkeypatch):
    from streamrip.verify import Result

    asked = []

    async def check(config):
        asked.append(1)
        return [
            Result(
                "qobuz", "ok", "x", plan="Paid", formats="FLAC up to 24-bit/192 kHz"
            ),
            Result("deezer", "ok", "x", plan="Free", formats="MP3 128"),
        ]

    monkeypatch.setattr(runner_module, "verify_logins", check)

    first = client.get("/accounts/overview", headers=JSON).get_json()
    client.get("/accounts/overview", headers=JSON)  # the last check is still good
    client.get("/accounts/overview?refresh=1", headers=JSON)

    assert first["qobuz"] == {
        "state": "ok",
        "plan": "Paid",
        "formats": "FLAC up to 24-bit/192 kHz",
    }
    assert first["deezer"]["plan"] == "Free"
    assert len(asked) == 2  # the second answer came from the first check


def test_qobuz_logs_in_with_a_script_run_on_its_web_player(client, runner):
    job_id = started(client.post("/accounts/qobuz/script", json={}, headers=JSON))
    script = wait_for(client, job_id, lambda job: job["script"])["script"]
    challenge = re.search(r'challenge: "([^"]+)"', script)[1]
    # Saving another service's app, or pasting an address, leaves it waiting.
    assert (
        client.post("/accounts/spotify/app", json={"client_id": "x"}).status_code == 200
    )
    paste = client.post(f"/jobs/{job_id}/paste", json={"address": "x"}, headers=JSON)
    assert paste.status_code == 404
    target = re.search(r'const target = "([^"]+)"', script)[1]
    assert target.endswith("/accounts/qobuz/receive")
    assert client.get(target).status_code == 200  # where the script moves the tab
    post = {"user_id": "42", "token": "TOKEN", "challenge": challenge}

    # Only the script this login showed has its challenge.
    guess = client.post(
        "/accounts/qobuz/capture", json={**post, "challenge": "guess"}, headers=JSON
    )
    assert guess.status_code == 400
    assert (
        client.post("/accounts/qobuz/capture", json=post, headers=JSON).status_code
        == 200
    )
    assert (
        client.post("/accounts/qobuz/capture", json=post, headers=JSON).status_code
        == 400
    )  # once

    done = wait_for(client, job_id)
    assert (done["status"], done["message"]) == ("done", "Logged in to Qobuz.")
    assert runner.logins == [("qobuz", "42", "TOKEN")]


def test_nothing_is_taken_when_no_qobuz_login_waits(client, runner):
    post = {"user_id": "42", "token": "TOKEN", "challenge": "anything"}

    assert (
        client.post("/accounts/qobuz/capture", json=post, headers=JSON).status_code
        == 400
    )
    assert runner.logins == []


def test_a_qobuz_login_given_up_on_ends(client):
    job_id = started(client.post("/accounts/qobuz/script", json={}, headers=JSON))
    wait_for(client, job_id, lambda job: job["script"])

    client.post(f"/jobs/{job_id}/cancel", headers=JSON)

    ended = wait_for(client, job_id)
    assert (ended["status"], ended["message"]) == (
        "error",
        "The Qobuz login was cancelled.",
    )
    assert ended["script"] == ""


@pytest.mark.parametrize("how", ["cancel", "app changed"])
def test_a_waiting_login_ends_when_cancelled_or_when_its_app_changes(
    client, monkeypatch, how
):
    def wait_for_redirect(_uri, _timeout, stop):
        if not stop.wait(5):
            raise SpotifyLoginError("Timed out waiting for the login to finish.")
        raise SpotifyLoginError("The login was cancelled.")

    monkeypatch.setattr("streamrip.web.logins._wait_for_redirect", wait_for_redirect)
    job_id = started(client.post("/accounts/spotify/login", headers=JSON))
    wait_for(client, job_id, lambda job: job["link"])
    if how == "cancel":
        client.post(f"/jobs/{job_id}/cancel", headers=JSON)
    else:
        client.post("/accounts/spotify/app", data={"client_id": "x"})
    job = wait_for(client, job_id)
    assert (job["status"], job["message"]) == ("error", "The login was cancelled.")


def test_checking_all_logins_is_a_job_that_reports_each(client, monkeypatch):
    async def fake(_config, ids=None):
        return [
            Verified("qobuz", "ok", "Studio, hi-res"),
            Verified("deezer", "failed", "expired", "streamrip login deezer"),
        ]

    monkeypatch.setattr(runner_module, "verify_logins", fake)
    job = run(client, "/accounts/verify")
    assert job["message"] == "1 of 2 are fine; log in again to Deezer."
    assert [(line["level"], line["text"]) for line in job["log"]] == [
        ("info", "Qobuz: ok, Studio, hi-res"),
        ("error", "Deezer: failed, expired"),
    ]


def test_export_runs_as_a_job_and_then_downloads_the_csv(client, services):
    job = run(client, "/playlists/spotify/export", json={"playlist": None})
    assert (job["status"], job["message"]) == (
        "done",
        "Liked Songs: 2 tracks exported.",
    )
    download = client.get(f"/jobs/{job['id']}/download")
    assert download.mimetype == "text/csv"
    assert "attachment" in download.headers["Content-Disposition"]
    assert csv_tracks(download) == ["Song A", "Nope"]

    services.providers["tidal"].existing_playlist("Röad trip", track("In list"))
    job = run(client, "/playlists/tidal/export", json={"playlist": "Röad trip"})
    disposition = client.get(f"/jobs/{job['id']}/download").headers[
        "Content-Disposition"
    ]
    # The accent for browsers that read filename*, an ASCII name for the rest.
    assert "filename*=UTF-8''R%C3%B6ad%20trip.csv" in disposition
    assert 'filename="Rad trip.csv"' in disposition

    job = run(client, "/playlists/tidal/export", json={"playlist": "missing"})
    assert job["status"] == "error"
    assert "No playlist" in job["message"]


def test_the_playlists_of_a_service_are_listed(client, services):
    services.providers["tidal"].existing_playlist("<i>Mix</i>", track("In list"))
    assert client.get("/playlists/tidal/list").get_json() == [
        {"id": "pl1", "name": "<i>Mix</i>", "track_count": 1, "readable": True}
    ]


def test_import_reports_what_was_not_found(client, services):
    job = run(client, "/playlists/tidal/import", **csv_upload(
        b"Artist A,Song A\nNobody,Nothing\n", playlist="Mix"
    ))  # fmt: skip
    assert job["message"] == "Mix: 1 matched, 1 not found; added 1"
    assert job["unmatched"] == [{"track": "Nobody - Nothing", "why": "not on Tidal"}]
    tidal = services.providers["tidal"]
    assert [tidal.native_id(t) for t in tidal.playlists_by_id["pl1"][1]] == ["1"]
    assert csv_tracks(client.get(f"/jobs/{job['id']}/unmatched.csv")) == ["Nothing"]


def test_import_can_add_albums(client, services):
    tidal = services.providers["tidal"]
    tidal.albums = [track("Discovery", "Daft Punk", ids={"tidal": "5"})]
    upload = csv_upload(b"Daft Punk,Discovery\n", "albums.csv", contains="albums")
    job = run(client, "/playlists/tidal/import", **upload)
    assert job["message"] == "Favourite albums: 1 matched, 0 not found; added 1"
    assert [a.ids for a in tidal.favorite_albums] == [{"tidal": "5"}]


@pytest.mark.parametrize(
    ("body", "message"), [(b"", "no tracks"), ("Björk,Jóga".encode("latin-1"), "UTF-8")]
)
def test_import_rejects_empty_and_non_utf8_files(client, body, message):
    response = client.post("/playlists/tidal/import", headers=JSON, **csv_upload(body))
    assert response.status_code == 400
    assert message in response.get_json()["message"]


def test_transfer_copies_liked_songs_and_can_go_to_the_favourites(client, services):
    job = run(client, "/playlists/transfer", json=TRANSFER)
    assert "Liked Songs (from Spotify): 1 matched, 1 not found" in job["message"]
    assert job["title"] == "Transfer from Spotify to Tidal"
    tidal = services.providers["tidal"]
    assert [name for name, _ in tidal.playlists_by_id.values()] == [
        "Liked Songs (from Spotify)"
    ]
    assert client.get(f"/jobs/{job['id']}/download").status_code == 404  # not an export

    run(client, "/playlists/transfer", json={**TRANSFER, "into": "favorites"})
    assert [tidal.native_id(t) for t in tidal.liked] == ["1"]


@pytest.mark.parametrize("body", [{"source": "tidal", "target": "tidal"}, {}])
def test_transfer_needs_two_different_services(client, body):
    response = client.post("/playlists/transfer", json=body, headers=JSON)
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


def test_a_transfer_can_report_how_tracks_matched(client):
    options = {**TRANSFER, "dry_run": "1", "min_score": "0.9"}
    too_high = {**options, "min_score": "2"}
    assert (
        client.post("/playlists/transfer", json=too_high, headers=JSON).status_code
        == 400
    )
    job = run(client, "/playlists/transfer", json=options)
    report = client.get(f"/jobs/{job['id']}/report.csv").get_data(as_text=True)
    assert report.splitlines()[0].startswith("result,how,score")
    assert "matched" in report
    assert "not found" in report


def test_a_failing_service_is_reported_and_a_bug_does_not_leak(client, services):
    spotify = services.providers["spotify"]

    def explode():
        raise ApiError(500, "The service fell over")

    spotify.liked_tracks = explode
    job = run(client, "/playlists/transfer", json=TRANSFER)
    assert job["status"] == "error"
    assert "fell over" in job["message"]

    def crash():
        raise RuntimeError("secret internal detail")

    spotify.liked_tracks = crash
    job = run(client, "/playlists/transfer", json=TRANSFER)
    assert job["status"] == "error"
    assert "secret internal detail" not in job["message"]


def test_everything_can_be_exported_as_m3u8_to_a_folder(client, services, tmp_path):
    folder = Path.home() / "exports"
    body = {
        "playlist": "*",
        "format": "m3u8",
        "folder": str(folder),
        "library": str(Path.home()),
    }
    job = run(client, "/playlists/spotify/export", json=body)
    assert job["status"] == "done", job["message"]
    assert list(folder.glob("*.m3u8"))
    outside = client.post(
        "/playlists/spotify/export", json={**body, "folder": "/etc"}, headers=JSON
    )
    assert outside.status_code == 400
    assert "inside your home folder" in outside.get_json()["message"]
    for odd in ("~/a\x00b", "~Music"):  # a NUL, a "~name" of no user
        sent = {**body, "library": odd}
        assert (
            client.post(
                "/playlists/spotify/export", json=sent, headers=JSON
            ).status_code
            == 400
        )

    # A downloads folder on another disk is the default, and allowed.
    music = tmp_path / "music"
    music.mkdir()
    services.config.session.downloads.folder = str(music)
    job = run(
        client, "/playlists/spotify/export", json={"playlist": "*", "format": "m3u8"}
    )
    assert job["status"] == "done", job["message"]
    assert list((music / "Playlists").glob("*.m3u8"))


def test_a_job_that_is_gone_is_a_404(client):
    assert client.get("/jobs/nope", headers=JSON).status_code == 404
    assert client.get("/jobs/nope/download").status_code == 404


def test_settings_change_the_config_and_a_broken_file_is_not_saved(client, services):
    options = {
        (s["name"], o["name"]): o
        for s in client.get("/settings/api").get_json()["sections"]
        for o in s["options"]
    }
    assert options["downloads", "max_connections"]["kind"] == "int"
    changes = {"downloads": {"max_connections": 3}, "cli": {"progress_bars": False}}
    assert client.post("/settings/api", json={"changes": changes}).status_code == 200
    config = services.config
    assert config.session.downloads.max_connections == 3
    assert config.session.cli.progress_bars is False
    assert "max_connections = 3" in config.path.read_text()
    wrong = {"changes": {"conversion": {"codec": "WMA"}}}  # not one of its values
    assert client.post("/settings/api", json=wrong).status_code == 400

    before = config.path.read_text()
    broken = {"text": "[downloads\n"}
    assert (
        client.post("/settings/file/api", json=broken, headers=JSON).status_code == 400
    )
    assert config.path.read_text() == before
