"""The pages and actions of the web interface: downloads and search, the
playlist tools, and the logins of every service."""

import io
from urllib.parse import quote
lazy from collections.abc import Callable

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
lazy from werkzeug.wrappers import Response as WerkzeugResponse

from ..rip.parse_url import parse_url
from ..sync.csvio import decode, parse_csv, slug, write_csv
from ..sync.engine import LIKED, import_albums, import_tracks, select_tracks
from ..sync.errors import CsvError, ProviderError
from ..sync.services import DEVELOPER_DASHBOARDS, LABELS, SERVICES
from .logins import BrowserLogin
from .runner import MEDIA_TYPES, SOURCES
lazy from ..config import Config
lazy from ..rip.main import Main
lazy from ..sync.models import Track
lazy from ..sync.services import Services
lazy from .jobs import Job, JobManager
lazy from .runner import Runner

pages = Blueprint("pages", __name__)
# What belongs to one playlist service lives under /playlists/spotify/... or
# /playlists/tidal/...
playlist_pages = Blueprint(
    "playlist", __name__, url_prefix=f"/playlists/<any({','.join(SERVICES)}):service>"
)

SOURCE_LABELS = {
    "qobuz": "Qobuz",
    "tidal": "Tidal",
    "deezer": "Deezer",
    "soundcloud": "SoundCloud",
    "spotify": "Spotify",
}
SOURCE_NOTES = {
    "qobuz": "FLAC up to 24/192",
    "tidal": "FLAC up to 24/192",
    "deezer": "FLAC 16/44.1",
    "soundcloud": "MP3 or AAC, no login",
    "spotify": "AAC from YouTube Music",
}


def _services() -> Services:
    return current_app.extensions["services"]


def _jobs() -> JobManager:
    return current_app.extensions["jobs"]


def _runner() -> Runner:
    return current_app.extensions["runner"]


def _config() -> Config:
    return current_app.extensions["config"]


def _sent() -> dict:
    """The JSON object the page sent; anything else counts as nothing sent."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _choice(value, choices, what: str) -> str:
    if value not in choices:
        raise ProviderError(f"Choose a {what}.")
    return value


def _start(kind: str, title: str, work: Callable[[Job], str]) -> tuple[Response, int]:
    """Run `work(job)` in the background. The page follows it at /jobs/<id>."""
    return jsonify(id=_jobs().start(kind, title, work).id), 202


def _job(job_id: str) -> Job:
    if (job := _jobs().get(job_id)) is None:
        abort(404, "Unknown job")
    return job


def _attachment(name: str, tracks: list[Track]) -> Response:
    """`tracks` as a CSV the browser saves as `name`.csv."""
    out = io.StringIO()
    write_csv(out, tracks)
    # filename* carries the real name (it may have accents); plain filename is
    # the ASCII fallback for old browsers.
    fallback = slug(name).encode("ascii", "ignore").decode().strip() or "export"
    disposition = f"attachment; filename=\"{fallback}.csv\"; filename*=UTF-8''{quote(slug(name))}.csv"
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": disposition},
    )


def _uploaded_tracks() -> tuple[str, list[Track]]:
    """The name and tracks of the CSV file the page sent."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        raise CsvError("Choose a CSV file first.")
    tracks = parse_csv(decode(upload.stream.read(), upload.filename), upload.filename)
    if not tracks:
        raise CsvError("There are no tracks in this file.")
    return upload.filename.rsplit(".", 1)[0] or "CSV list", tracks


def _accounts() -> list[dict]:
    """What the templates need to know about each playlist service."""
    services = _services()
    return [
        {
            "name": name,
            "label": LABELS[name],
            "configured": services.is_configured(name),
            "connected": services.is_logged_in(name),
        }
        for name in SERVICES
    ]


def _download_sources() -> list[dict]:
    logged_in = _runner().logged_in()
    return [
        {
            "name": s,
            "label": SOURCE_LABELS[s],
            "note": SOURCE_NOTES[s],
            "ready": logged_in[s],
        }
        for s in SOURCES
    ]


# --- pages ---------------------------------------------------------------------


@pages.get("/")
def dashboard() -> str:
    return render_template(
        "dashboard.html",
        sources=_download_sources(),
        media_types=MEDIA_TYPES,
        folder=_config().session.downloads.folder,
    )


@pages.get("/playlists")
def playlists_page() -> str:
    return render_template(
        "playlists.html", accounts=_accounts(), sources=_download_sources()
    )


@pages.get("/accounts")
def accounts() -> str:
    services = _services()
    return render_template(
        "accounts.html",
        sources=_download_sources(),
        accounts=_accounts(),
        dashboards=DEVELOPER_DASHBOARDS,
        redirect_uris={name: services.redirect_uri(name) for name in SERVICES},
        config_path=_config().path,
        tidal_secret=bool(services.client_secret("tidal")),
    )


# --- jobs ----------------------------------------------------------------------


@pages.get("/jobs/<job_id>")
def job_status(job_id: str) -> Response:
    return jsonify(_job(job_id).to_json())


@pages.get("/jobs/<job_id>/unmatched.csv")
def job_unmatched(job_id: str) -> Response:
    return _attachment("unmatched", [miss.track for miss in _job(job_id).misses])


@pages.get("/jobs/<job_id>/download")
def job_download(job_id: str) -> Response:
    """The CSV an export made."""
    if (download := _job(job_id).download) is None:
        abort(404, "This job has no file (yet).")
    return _attachment(*download)


@pages.post("/jobs/<job_id>/paste")
def job_paste(job_id: str) -> Response:
    """The address a browser login ended up on, pasted by hand."""
    _job(job_id)
    login: BrowserLogin | None = current_app.extensions["logins"].get(job_id)
    if login is None:
        abort(404, "This login is over.")
    login.paste(str(_sent().get("address") or ""))
    return jsonify(status="ok")


# --- downloading ---------------------------------------------------------------


@pages.post("/download")
def download() -> tuple[Response, int]:
    """Download links ({"urls": "..."}) or search results ({"items": [...]})."""
    data = _sent()
    if items := data.get("items"):
        try:
            chosen = [
                (
                    _choice(i["source"], SOURCES, "source"),
                    _choice(i["media_type"], MEDIA_TYPES, "kind"),
                    str(i["id"]),
                )
                for i in items
            ]
        except (KeyError, TypeError) as e:
            raise ProviderError("Choose what to download.") from e

        async def add(main: Main):
            await main.add_all_by_id(chosen)

        title = f"Downloading {len(chosen)} search result{'s' * (len(chosen) > 1)}"
    else:
        urls = list(dict.fromkeys(str(data.get("urls") or "").split()))
        if not urls:
            raise ProviderError("Paste one or more links first.")
        if bad := [url for url in urls if parse_url(url) is None]:
            raise ProviderError(f"Not a link streamrip knows: {bad[0][:200]}")

        async def add(main: Main):
            await main.add_all(urls)

        title = f"Downloading {len(urls)} link{'s' * (len(urls) > 1)}"
    runner = _runner()
    return _start("download", title, lambda job: runner.download(job, add))


@pages.post("/download/csv")
def download_csv() -> tuple[Response, int]:
    """A CSV list, each track searched on a source (as `streamrip csv`)."""
    name, tracks = _uploaded_tracks()
    source = _choice(request.form.get("source"), SOURCES, "source")
    fallback = request.form.get("fallback") or None
    if fallback is not None:
        _choice(fallback, SOURCES, "fallback source")
    runner = _runner()

    async def add(main: Main):
        await main.resolve_csv(name, tracks, source, fallback)

    return _start(
        "download",
        f"Downloading {name} from {SOURCE_LABELS[source]}",
        lambda job: runner.download(job, add),
    )


@pages.post("/search")
def search() -> Response:
    data = _sent()
    source = _choice(data.get("source"), SOURCES, "source")
    media_type = _choice(data.get("media_type"), MEDIA_TYPES, "kind")
    if not (query := str(data.get("query") or "").strip()):
        raise ProviderError("Type what to search for.")
    results = _runner().search(source, media_type, query[:200])
    return jsonify(
        [
            {
                "source": r.source,
                "media_type": r.media_type,
                "id": r.id,
                "name": r.name,
                "artist": r.artist,
                "year": r.year,
                "explicit": r.explicit,
                "details": r.details,
                "image": r.image
                if r.image and r.image.startswith("https://")
                else None,
            }
            for r in results
        ]
    )


# --- the playlist tools --------------------------------------------------------


@pages.post("/playlists/transfer")
def transfer() -> tuple[Response, int]:
    data = _sent()
    source = _choice(data.get("source"), SERVICES, "service to copy from")
    target = _choice(data.get("target"), SERVICES, "service to copy to")
    if source == target:
        raise ProviderError("Choose two different services.")
    playlist = str(data.get("playlist") or "") or None  # str: an id may be a number
    name = str(data.get("name") or "").strip()
    favorites = data.get("into") == "favorites"
    source_provider = _services().provider(source)
    target_provider = _services().provider(target)

    def work(job: Job) -> str:
        ((source_name, tracks),) = select_tracks(
            source_provider,
            liked=not playlist,
            playlist=playlist,
            progress=job.progress,
        )
        if favorites:
            destination = None  # the target's favourites
        else:
            destination = name or (
                f"{LIKED} (from {LABELS[source]})"
                if source_name == LIKED
                else source_name
            )
        return import_tracks(
            target_provider, tracks, destination, progress=job.progress
        ).summary()

    return _start(
        "transfer", f"Transfer from {LABELS[source]} to {LABELS[target]}", work
    )


@playlist_pages.get("/list")
def playlist_list(service: str) -> Response:
    return jsonify(
        [
            {
                "id": p.id,
                "name": p.name,
                "track_count": p.track_count,
                "readable": p.readable,
            }
            for p in _services().provider(service).playlists()
        ]
    )


@playlist_pages.post("/export")
def export(service: str) -> tuple[Response, int]:
    """Read liked songs or a playlist; the page then saves the CSV."""
    playlist = str(_sent().get("playlist") or "") or None
    provider = _services().provider(service)  # here: a missing app fails the request

    def work(job: Job) -> str:
        ((name, tracks),) = select_tracks(
            provider, liked=not playlist, playlist=playlist, progress=job.progress
        )
        job.download = (name, tracks)
        return f"{name}: {len(tracks)} tracks exported."

    return _start("export", f"Export from {LABELS[service]}", work)


@playlist_pages.post("/import")
def import_csv(service: str) -> tuple[Response, int]:
    _name, tracks = _uploaded_tracks()
    playlist = request.form.get("playlist", "").strip() or "streamrip import"
    into_favorites = request.form.get("into") == "favorites"
    albums = request.form.get("contains") == "albums"  # a row per album
    provider = _services().provider(service)

    def work(job: Job) -> str:
        if albums:
            return import_albums(provider, tracks, progress=job.progress).summary()
        destination = None if into_favorites else playlist
        return import_tracks(
            provider, tracks, destination, progress=job.progress
        ).summary()

    return _start("import", f"Import into {LABELS[service]}", work)


@playlist_pages.post("/download")
def playlist_download(service: str) -> tuple[Response, int]:
    """Read liked songs or a playlist, and download it from a source."""
    data = _sent()
    playlist = str(data.get("playlist") or "") or None
    source = _choice(data.get("source"), SOURCES, "source to download from")
    fallback = data.get("fallback") or None
    if fallback is not None:
        _choice(fallback, SOURCES, "fallback source")
    provider = _services().provider(service)
    runner = _runner()

    def work(job: Job) -> str:
        ((name, tracks),) = select_tracks(
            provider, liked=not playlist, playlist=playlist, progress=job.progress
        )

        async def add(main: Main):
            await main.resolve_csv(name, tracks, source, fallback)

        return runner.download(job, add)

    title = f"Downloading from {LABELS[service]}, via {SOURCE_LABELS[source]}"
    return _start("playlist-download", title, work)


# --- logging in ----------------------------------------------------------------


def _back(message: str, category: str = "ok"):
    flash(message, category)
    return redirect(url_for("pages.accounts"))


def _form(name: str) -> str:
    return request.form.get(name, "").strip()


@pages.post("/accounts/qobuz")
def login_qobuz() -> WerkzeugResponse:
    user_id, token = _form("user_id"), _form("auth_token")
    if not user_id or not token:
        return _back("Fill in both the user id and the token.", "error")
    _runner().login_qobuz(user_id, token)
    return _back("Logged in to Qobuz.")


@pages.post("/accounts/deezer")
def login_deezer() -> WerkzeugResponse:
    if not (arl := _form("arl")):
        return _back("Paste the ARL cookie first.", "error")
    _runner().login_deezer(arl)
    return _back("Logged in to Deezer.")


@pages.post("/accounts/tidal")
def login_tidal() -> tuple[Response, int]:
    runner = _runner()
    return _start("login", "Log in to Tidal (downloads)", runner.login_tidal)


@pages.post("/accounts/<any(spotify, tidal):service>/app")
def set_app(service: str) -> WerkzeugResponse:
    """The client id (and for Tidal the secret) of the user's developer app."""
    client_id, secret = _form("client_id"), _form("client_secret")
    if not client_id or (
        service == "tidal" and not secret and not _services().client_secret("tidal")
    ):
        return _back(
            "Fill in the Client ID" + (" and Secret." if service == "tidal" else "."),
            "error",
        )
    config = _config()
    for data in (config.session, config.file):
        if service == "spotify":
            data.spotify.client_id = client_id
        else:
            data.tidal.api_client_id = client_id
            data.tidal.api_client_secret = secret or data.tidal.api_client_secret
    config.file.set_modified()
    config.save_file()
    return _back(f"Saved the {LABELS[service]} app. Now log in.")


@pages.post("/accounts/<any(spotify, tidal):service>/login")
def browser_login(service: str) -> tuple[Response, int]:
    services = _services()
    login = BrowserLogin(services.oauth(service), services.store)
    logins = current_app.extensions["logins"]

    def work(job: Job) -> str:
        logins[job.id] = login
        try:
            return login.run(job)
        finally:
            logins.pop(job.id, None)

    what = "" if service == "spotify" else " (playlists)"
    return _start("login", f"Log in to {LABELS[service]}{what}", work)


@pages.post("/accounts/<any(spotify, tidal):service>/logout")
def logout(service: str) -> WerkzeugResponse:
    _services().store.delete(service)
    return _back(f"Logged out of {LABELS[service]}.")
