"""What the pages of the web interface share: the parts of the running app, and
the way a request starts a background job."""

from pathlib import Path
lazy from collections.abc import Callable

from flask import Response, abort, current_app, jsonify, request, send_from_directory

from ..sync.errors import ProviderError
lazy from ..config import Config
lazy from ..sync.services import Services
lazy from .jobs import Job, JobManager
lazy from .runner import Runner

# The interface itself: the Svelte app frontend/ builds (`npm run build`), served
# as it is, so running streamrip needs no Node.
APP = Path(__file__).parent / "static" / "app"

SOURCE_LABELS = frozendict(
    {
        "qobuz": "Qobuz",
        "tidal": "Tidal",
        "deezer": "Deezer",
        "soundcloud": "SoundCloud",
        "spotify": "Spotify",
    }
)


def spa() -> Response:
    """A page of the interface: the app, which asks for the page's data itself."""
    return send_from_directory(APP, "index.html")


def get_services() -> Services:
    return current_app.extensions["services"]


def get_jobs() -> JobManager:
    return current_app.extensions["jobs"]


def get_runner() -> Runner:
    return current_app.extensions["runner"]


def get_config() -> Config:
    return current_app.extensions["config"]


def sent() -> dict:
    """The JSON object the page sent; anything else counts as nothing sent."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def payload() -> dict:
    """What the page sent: JSON, or a form (which may carry a file)."""
    return sent() or request.form.to_dict()


def chain(
    data: dict, sources, first: str = "fallback", then: str = "then"
) -> str | None:
    """The fallback chain the two selects of a form name, "tidal,deezer", or None."""
    names = [str(data.get(key) or "") for key in (first, then)]
    names = [n for n in names if n]
    for name in names:
        choice(name, sources, "fallback source")
    return ",".join(dict.fromkeys(names)) or None


def choice(value, choices, what: str) -> str:
    if value not in choices:
        raise ProviderError(f"Choose a {what}.")
    return value


def start(kind: str, title: str, work: Callable[[Job], str]) -> tuple[Response, int]:
    """Run `work(job)` in the background. The page follows it at /jobs/<id>."""
    return jsonify(id=get_jobs().start(kind, title, work).id), 202


def find_job(job_id: str) -> Job:
    if (job := get_jobs().get(job_id)) is None:
        abort(404, "Unknown job")
    return job
