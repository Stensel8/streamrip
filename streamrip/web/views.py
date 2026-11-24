"""The pages and actions of the web interface: downloads and search, the
playlist tools, and the logins of every service."""

import io
from pathlib import PureWindowsPath
from urllib.parse import quote

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
)

from .. import __version__
from ..accounts import LIBRARY_LOGINS, Accounts
from ..rip.parse_url import parse_url
from ..sync.csvio import decode, parse_csv, slug, write_csv
from ..sync.engine import LIKED, import_albums, import_tracks, select_tracks
from ..sync.errors import CsvError, ProviderError
from ..sync.services import LABELS, SERVICES
from .common import (
    SOURCE_LABELS,
    choice,
    find_job,
    get_config,
    get_jobs,
    get_runner,
    get_services,
    sent,
    start,
)
from .runner import MEDIA_TYPES, SOURCES
lazy from ..rip.main import Main
lazy from ..sync.models import Track
lazy from .jobs import Job
lazy from .logins import BrowserLogin

pages = Blueprint("pages", __name__)
# What belongs to one playlist service lives under /playlists/spotify/... or
# /playlists/tidal/...
playlist_pages = Blueprint(
    "playlist", __name__, url_prefix=f"/playlists/<any({','.join(SERVICES)}):service>"
)

SOURCE_NOTES = frozendict(
    {
        "qobuz": "FLAC up to 24/192",
        "tidal": "FLAC up to 24/192",
        "deezer": "FLAC 16/44.1",
        "soundcloud": "MP3 or AAC, no login",
        "spotify": "AAC from YouTube Music",
    }
)


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
    # Only the name: a browser may send a whole path, with either separator.
    return PureWindowsPath(upload.filename).stem or "CSV list", tracks


def _connected_services() -> list[dict]:
    """The playlist services that are logged in, for the selects of the playlist page."""
    accounts = Accounts(get_config())
    return [
        {"name": name, "label": LABELS[name]}
        for name, login_id in LIBRARY_LOGINS.items()
        if accounts.logged_in(login_id)
    ]


def _download_sources() -> list[dict]:
    accounts = Accounts(get_config())
    return [
        {
            "name": s,
            "label": SOURCE_LABELS[s],
            "note": SOURCE_NOTES[s],
            "ready": s == "soundcloud" or accounts.logged_in(s),  # it needs none
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
        folder=get_config().session.downloads.folder,
    )


@pages.get("/playlists")
def playlists_page() -> str:
    return render_template(
        "playlists.html", accounts=_connected_services(), sources=_download_sources()
    )


@pages.get("/health")
def health() -> Response:
    """That the server answers, for a container's health check."""
    return jsonify(status="ok", version=__version__)


# --- jobs ----------------------------------------------------------------------


@pages.get("/jobs/running")
def job_running() -> Response:
    """The job that is going now, so a reloaded page can pick it up again."""
    job = get_jobs().running()
    return jsonify(id=job.id if job else None)


@pages.get("/jobs/<job_id>")
def job_status(job_id: str) -> Response:
    return jsonify(find_job(job_id).to_json())


@pages.get("/jobs/<job_id>/unmatched.csv")
def job_unmatched(job_id: str) -> Response:
    return _attachment("unmatched", [miss.track for miss in find_job(job_id).misses])


@pages.get("/jobs/<job_id>/download")
def job_download(job_id: str) -> Response:
    """The CSV an export made."""
    if (download := find_job(job_id).download) is None:
        abort(404, "This job has no file (yet).")
    return _attachment(*download)


@pages.post("/jobs/<job_id>/cancel")
def job_cancel(job_id: str) -> Response:
    """Give up on a browser login, so another can start."""
    find_job(job_id)
    if (login := current_app.extensions["logins"].get(job_id)) is not None:
        login.cancel()
    return jsonify(status="ok")


@pages.post("/jobs/<job_id>/paste")
def job_paste(job_id: str) -> Response:
    """The address a browser login ended up on, pasted by hand."""
    find_job(job_id)
    login: BrowserLogin | None = current_app.extensions["logins"].get(job_id)
    if login is None:
        abort(404, "This login is over.")
    login.paste(str(sent().get("address") or ""))
    return jsonify(status="ok")


# --- downloading ---------------------------------------------------------------


@pages.post("/download")
def download() -> tuple[Response, int]:
    """Download links ({"urls": "..."}) or search results ({"items": [...]})."""
    data = sent()
    if items := data.get("items"):
        try:
            chosen = [
                (
                    choice(i["source"], SOURCES, "source"),
                    choice(i["media_type"], MEDIA_TYPES, "kind"),
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
    runner = get_runner()
    return start("download", title, lambda job: runner.download(job, add))


@pages.post("/download/csv")
def download_csv() -> tuple[Response, int]:
    """A CSV list, each track searched on a source (as `streamrip csv`)."""
    name, tracks = _uploaded_tracks()
    source = choice(request.form.get("source"), SOURCES, "source")
    fallback = request.form.get("fallback") or None
    if fallback is not None:
        choice(fallback, SOURCES, "fallback source")
    runner = get_runner()

    async def add(main: Main):
        await main.resolve_csv(name, tracks, source, fallback)

    return start(
        "download",
        f"Downloading {name} from {SOURCE_LABELS[source]}",
        lambda job: runner.download(job, add),
    )


@pages.post("/search")
def search() -> Response:
    data = sent()
    source = choice(data.get("source"), SOURCES, "source")
    media_type = choice(data.get("media_type"), MEDIA_TYPES, "kind")
    if not (query := str(data.get("query") or "").strip()):
        raise ProviderError("Type what to search for.")
    results = get_runner().search(source, media_type, query[:200])
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
    data = sent()
    source = choice(data.get("source"), SERVICES, "service to copy from")
    target = choice(data.get("target"), SERVICES, "service to copy to")
    if source == target:
        raise ProviderError("Choose two different services.")
    playlist = str(data.get("playlist") or "") or None  # str: an id may be a number
    name = str(data.get("name") or "").strip()
    favorites = data.get("into") == "favorites"
    services = get_services()  # the job runs on a thread of its own: no request there
    source_provider = services.provider(source)
    target_provider = services.provider(target)

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
            target_provider,
            tracks,
            destination,
            progress=job.progress,
            musicbrainz=services.musicbrainz(),
        ).summary()

    return start(
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
            for p in get_services().provider(service).playlists()
        ]
    )


@playlist_pages.post("/export")
def export(service: str) -> tuple[Response, int]:
    """Read liked songs or a playlist; the page then saves the CSV."""
    playlist = str(sent().get("playlist") or "") or None
    provider = get_services().provider(service)  # here: a missing app fails the request

    def work(job: Job) -> str:
        ((name, tracks),) = select_tracks(
            provider, liked=not playlist, playlist=playlist, progress=job.progress
        )
        job.download = (name, tracks)
        return f"{name}: {len(tracks)} tracks exported."

    return start("export", f"Export from {LABELS[service]}", work)


@playlist_pages.post("/import")
def import_csv(service: str) -> tuple[Response, int]:
    _name, tracks = _uploaded_tracks()
    playlist = request.form.get("playlist", "").strip() or "streamrip import"
    into_favorites = request.form.get("into") == "favorites"
    albums = request.form.get("contains") == "albums"  # a row per album
    services = get_services()
    provider = services.provider(service)

    def work(job: Job) -> str:
        if albums:
            return import_albums(provider, tracks, progress=job.progress).summary()
        destination = None if into_favorites else playlist
        return import_tracks(
            provider,
            tracks,
            destination,
            progress=job.progress,
            musicbrainz=services.musicbrainz(),
        ).summary()

    return start("import", f"Import into {LABELS[service]}", work)


@playlist_pages.post("/download")
def playlist_download(service: str) -> tuple[Response, int]:
    """Read liked songs or a playlist, and download it from a source."""
    data = sent()
    playlist = str(data.get("playlist") or "") or None
    source = choice(data.get("source"), SOURCES, "source to download from")
    fallback = data.get("fallback") or None
    if fallback is not None:
        choice(fallback, SOURCES, "fallback source")
    provider = get_services().provider(service)
    runner = get_runner()

    def work(job: Job) -> str:
        ((name, tracks),) = select_tracks(
            provider, liked=not playlist, playlist=playlist, progress=job.progress
        )

        async def add(main: Main):
            await main.resolve_csv(name, tracks, source, fallback)

        return runner.download(job, add)

    title = f"Downloading from {LABELS[service]}, via {SOURCE_LABELS[source]}"
    return start("playlist-download", title, work)
