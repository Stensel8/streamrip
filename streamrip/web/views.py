"""The pages and actions of the web interface: downloads and search, the
playlist tools, and the logins of every service."""

import io
import os
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, urlsplit

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    request,
)

from .. import __version__
from ..accounts import LIBRARY_LOGINS, Accounts
from ..client import CLIENTS
from ..console import plural
from ..rip.parse_url import parse_url
from ..sync.csvio import decode, parse_csv, slug, write_csv
from ..sync.engine import LIKED, import_albums, import_tracks, select_tracks
from ..sync.errors import CsvError, ProviderError
from ..sync.export import export_lists
from ..sync.report import report_csv
from ..sync.services import LABELS, SERVICES
from .common import (
    SOURCE_LABELS,
    chain,
    choice,
    find_job,
    get_config,
    get_jobs,
    get_runner,
    get_services,
    payload,
    sent,
    spa,
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

# What `streamrip id` takes: a label has no search, but it has an id.
ID_KINDS = (*MEDIA_TYPES, "label")
SOURCE_NOTES = frozendict(
    {
        "qobuz": "FLAC up to 24/192",
        "tidal": "FLAC up to 24/192",
        "deezer": "FLAC 16/44.1",
        "soundcloud": "MP3 or AAC, better with a login",
        "spotify": "its own audio (Soloist), else AAC from YouTube Music",
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
            # What it can be searched for: not every source has all four.
            "kinds": [t for t in MEDIA_TYPES if t in CLIENTS[s].search_types],
            "ready": s == "soundcloud" or accounts.logged_in(s),  # it needs none
        }
        for s in SOURCES
    ]


# --- pages ---------------------------------------------------------------------


@pages.get("/")
@pages.get("/playlists")
def page() -> Response:
    return spa()


@pages.get("/download/api")
def download_data() -> Response:
    return jsonify(
        sources=_download_sources(),
        media_types=MEDIA_TYPES,
        id_kinds=ID_KINDS,
        folder=get_config().session.downloads.folder,
    )


@pages.get("/playlists/api")
def playlists_data() -> Response:
    return jsonify(services=_connected_services(), sources=_download_sources())


@pages.get("/health")
def health() -> Response:
    """That the server answers, for a container's health check."""
    return jsonify(status="ok", version=__version__)


# --- jobs ----------------------------------------------------------------------


@pages.get("/jobs/running")
def job_running() -> Response:
    """The job that is going now, so a reloaded page can pick it up again."""
    kinds = set(request.args.get("kinds", "").split())
    job = get_jobs().running(kinds)
    return jsonify(id=job.id if job else None)


@pages.get("/jobs/<job_id>")
def job_status(job_id: str) -> Response:
    return jsonify(find_job(job_id).to_json())


@pages.get("/jobs/<job_id>/unmatched.csv")
def job_unmatched(job_id: str) -> Response:
    return _attachment("unmatched", [miss.track for miss in find_job(job_id).misses])


@pages.get("/jobs/<job_id>/report.csv")
def job_report(job_id: str) -> Response:
    """How each track was matched, as `--report` writes it."""
    job = find_job(job_id)
    out = io.StringIO()
    report_csv(out, job.matched, job.misses)
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="report.csv"'},
    )


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
    login = current_app.extensions["logins"].get(job_id)
    if not isinstance(login, BrowserLogin):  # over, or one without an address (Qobuz)
        abort(404, "No login here waits for an address.")
    login.paste(str(sent().get("address") or ""))
    return jsonify(status="ok")


# --- downloading ---------------------------------------------------------------


def _matching(data) -> tuple[bool, float]:
    """The options of a transfer or an import: only look, and how sure a match must be."""
    given = data.get("min_score")
    try:
        score = 0.8 if given in {None, ""} else float(given)
    except TypeError, ValueError:
        score = -1.0
    if not 0 <= score <= 1:
        raise ProviderError("The score a match needs is between 0 and 1.")
    return bool(data.get("dry_run")), score


def _download_or_preview(title: str, add, data) -> tuple[Response, int]:
    """Download what `add` brings, or only say what that would do (`plan`)."""
    runner = get_runner()
    if data.get("plan"):
        return start("plan", f"Preview: {title}", lambda job: runner.plan(job, add))
    return start(
        "download", f"Downloading {title}", lambda job: runner.download(job, add)
    )


def _links_in(data: dict) -> list[str]:
    """The links typed in, and those in a file, once each."""
    text = str(data.get("urls") or "")
    if (upload := request.files.get("file")) and upload.filename:
        text += "\n" + decode(upload.stream.read(), upload.filename)
    return list(dict.fromkeys(text.split()))


@pages.post("/download")
def download() -> tuple[Response, int]:
    """Download links, typed or from a file, or search results ({"items": [...]})."""
    data = payload()
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

        title = f"{len(chosen)} search result{'s' * (len(chosen) > 1)}"
    else:
        urls = _links_in(data)
        if not urls:
            raise ProviderError("Paste one or more links, or choose a file with links.")
        if bad := [url for url in urls if parse_url(url) is None]:
            raise ProviderError(f"Not a link streamrip knows: {bad[0][:200]}")

        async def add(main: Main):
            await main.add_all(urls)

        title = f"{len(urls)} link{'s' * (len(urls) > 1)}"
    return _download_or_preview(title, add, data)


@pages.post("/download/id")
def download_by_id() -> tuple[Response, int]:
    """One item by its id on a source (as `streamrip id`)."""
    data = payload()
    source = choice(data.get("source"), SOURCES, "source")
    media_type = choice(data.get("media_type"), ID_KINDS, "kind")
    if not (item_id := str(data.get("id") or "").strip()):
        raise ProviderError("Type the id.")

    async def add(main: Main):
        await main.add_by_id(source, media_type, item_id)

    title = f"{media_type} {item_id[:40]} from {SOURCE_LABELS[source]}"
    return _download_or_preview(title, add, data)


@pages.post("/download/lastfm")
def download_lastfm() -> tuple[Response, int]:
    """The tracks of a Last.fm playlist, searched on a source (as `streamrip lastfm`)."""
    data = payload()
    url = str(data.get("url") or "").strip()
    if (urlsplit(url).hostname or "").removeprefix("www.") != "last.fm":
        raise ProviderError("Paste the address of a last.fm playlist.")
    source = choice(data.get("source"), SOURCES, "source")
    fallback = chain(data, SOURCES)

    async def add(main: Main):
        lastfm = main.config.session.lastfm
        lastfm.source, lastfm.fallback_source = source, fallback or ""
        await main.resolve_lastfm(url)

    title = f"a last.fm playlist from {SOURCE_LABELS[source]}"
    return _download_or_preview(title, add, data)


@pages.post("/download/csv")
def download_csv() -> tuple[Response, int]:
    """A CSV list, each track searched on a source (as `streamrip csv`)."""
    name, tracks = _uploaded_tracks()
    data = payload()
    source = choice(data.get("source"), SOURCES, "source")
    fallback = chain(data, SOURCES)

    async def add(main: Main):
        await main.resolve_csv(name, tracks, source, fallback)

    title = f"{name} from {SOURCE_LABELS[source]}"
    return _download_or_preview(title, add, data)


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
    dry_run, min_score = _matching(data)
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
        result = import_tracks(
            target_provider,
            tracks,
            destination,
            min_score=min_score,
            dry_run=dry_run,
            progress=job.progress,
            musicbrainz=services.musicbrainz(),
        )
        job.matched = result.matched
        return result.summary()

    title = f"Transfer from {LABELS[source]} to {LABELS[target]}"
    return start(
        "transfer",
        f"Preview: {title[0].lower()}{title[1:]}" if dry_run else title,
        work,
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


def _folder(value, what: str, default: Path) -> Path:
    """A folder the page named, inside the home folder or the downloads folder,
    which may be on another disk (a page must not write anywhere)."""
    refused = ProviderError(
        f"Choose a {what} inside your home folder or the downloads folder."
    )
    try:
        path = os.path.realpath(Path(str(value or "").strip() or default).expanduser())
    except (ValueError, RuntimeError) as e:  # a NUL, or "~name" of no user
        raise refused from e
    roots = [Path.home()]
    if downloads := get_config().session.downloads.folder:
        roots.append(Path(downloads).expanduser())
    # Both with their links followed, then compared as text (the check CodeQL
    # knows): a root itself, or something inside one.
    real = [os.path.realpath(root) for root in roots]
    if not any(path == root or path.startswith(root + os.sep) for root in real):
        raise refused
    return Path(path)


@playlist_pages.post("/export")
def export(service: str) -> tuple[Response, int]:
    """Read liked songs, a playlist or all of them. One list as CSV is saved by
    the page; the rest (everything, or M3U8) is written to a folder here."""
    data = sent()
    playlist = str(data.get("playlist") or "") or None
    everything = playlist == "*"
    m3u8 = data.get("format") == "m3u8"
    provider = get_services().provider(service)  # here: a missing app fails the request
    downloads = Path(get_config().session.downloads.folder).expanduser()
    library = _folder(data.get("library"), "music folder", downloads) if m3u8 else None
    if library is not None and not library.is_dir():
        raise ProviderError(f"{library} is no folder.")
    folder = (
        _folder(data.get("folder"), "folder to save in", downloads / "Playlists")
        if m3u8 or everything
        else None
    )

    def work(job: Job) -> str:
        selected = select_tracks(
            provider,
            liked=not playlist,
            playlist=None if everything else playlist,
            progress=job.progress,
        )
        if folder is None:
            ((name, tracks),) = selected
            job.download = (name, tracks)
            return f"{name}: {len(tracks)} tracks exported."
        lines, missing = export_lists(
            selected,
            everything=everything,
            output=folder if everything else folder / f"{slug(selected[0][0])}.m3u8",
            service=service,
            m3u8=m3u8,
            library=library,
            say=lambda line: job.log.append(("info", line)),
        )
        for line in lines:
            job.log.append(("info", line))
        return f"{plural(len(lines), 'file')} written in {folder}." + (
            f" {len(missing)} tracks are not in the music folder." if missing else ""
        )

    return start("export", f"Export from {LABELS[service]}", work)


@playlist_pages.post("/import")
def import_csv(service: str) -> tuple[Response, int]:
    _name, tracks = _uploaded_tracks()
    playlist = request.form.get("playlist", "").strip() or "streamrip import"
    into_favorites = request.form.get("into") == "favorites"
    albums = request.form.get("contains") == "albums"  # a row per album
    dry_run, min_score = _matching(request.form)
    services = get_services()
    provider = services.provider(service)

    def work(job: Job) -> str:
        how = {"min_score": min_score, "dry_run": dry_run, "progress": job.progress}
        if albums:
            result = import_albums(provider, tracks, **how)
        else:
            destination = None if into_favorites else playlist
            result = import_tracks(
                provider, tracks, destination, musicbrainz=services.musicbrainz(), **how
            )
        job.matched = result.matched
        return result.summary()

    title = f"Import into {LABELS[service]}"
    return start(
        "import", f"Preview: {title[0].lower()}{title[1:]}" if dry_run else title, work
    )


@playlist_pages.post("/download")
def playlist_download(service: str) -> tuple[Response, int]:
    """Read liked songs or a playlist, and download it from a source."""
    data = sent()
    playlist = str(data.get("playlist") or "") or None
    source = choice(data.get("source"), SOURCES, "source to download from")
    fallback = chain(data, SOURCES)
    preview = bool(data.get("plan"))
    provider = get_services().provider(service)
    runner = get_runner()

    def work(job: Job) -> str:
        ((name, tracks),) = select_tracks(
            provider, liked=not playlist, playlist=playlist, progress=job.progress
        )

        async def add(main: Main):
            await main.resolve_csv(name, tracks, source, fallback)

        return (runner.plan if preview else runner.download)(job, add)

    title = f"from {LABELS[service]}, via {SOURCE_LABELS[source]}"
    if preview:
        # A kind of its own: the Download page's previews are "plan".
        return start("playlist-plan", f"Preview: {title}", work)
    return start("playlist-download", f"Downloading {title}", work)
