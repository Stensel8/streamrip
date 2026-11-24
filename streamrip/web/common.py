"""What the pages of the web interface share: the parts of the running app, and
the way a request starts a background job."""

lazy from collections.abc import Callable

from flask import Response, abort, current_app, jsonify, request

from ..sync.errors import ProviderError
lazy from ..config import Config
lazy from ..sync.services import Services
lazy from .jobs import Job, JobManager
lazy from .runner import Runner

SOURCE_LABELS = frozendict(
    {
        "qobuz": "Qobuz",
        "tidal": "Tidal",
        "deezer": "Deezer",
        "soundcloud": "SoundCloud",
        "spotify": "Spotify",
    }
)


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


def form(name: str) -> str:
    return request.form.get(name, "").strip()


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
