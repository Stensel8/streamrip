"""The Library page: what streamrip has downloaded and what failed, with the
retry and the clearing of `streamrip repair` and `streamrip database`."""

from pathlib import Path

from flask import Blueprint, Response, jsonify

from .. import db
from ..console import plural
from .common import get_config, get_runner, sent, spa, start

library_pages = Blueprint("library", __name__, url_prefix="/library")

SHOWN = 300  # rows of a table the page lists


def _tables() -> tuple[db.Downloads, db.Failed]:
    c = get_config().session.database
    return db.Downloads(c.downloads_path), db.Failed(c.failed_downloads_path)


def _row(source: str, item_id: str, path: str) -> tuple[str, str, str]:
    """A downloads row as the page shows it: the file within the music folder."""
    folder = Path(get_config().session.downloads.folder)
    file = Path(path)
    return (
        source,
        item_id,
        str(file.relative_to(folder)) if file.is_relative_to(folder) else path,
    )


@library_pages.route("")
def page() -> Response:
    return spa()


@library_pages.get("/api")
def data() -> Response:
    downloads, failed = _tables()
    return jsonify(
        # Counted and limited by sqlite: a library may have many thousands.
        downloaded=downloads.count(),
        recent=[_row(*db.download_row(*row)) for row in downloads.recent(SHOWN)],
        failed=failed.count(),
        failures=failed.recent(SHOWN),
        shown=SHOWN,
    )


@library_pages.post("/repair")
def repair() -> tuple[Response, int]:
    """Retry the failed downloads."""
    flat = bool(sent().get("flat"))
    runner = get_runner()
    return start(
        "repair", "Retrying the failed downloads", lambda job: runner.repair(job, flat)
    )


@library_pages.post("/clear/<any(downloads, failed):table>")
def clear(table: str) -> Response:
    """Forget what was downloaded (it is downloaded again), or the failures."""
    downloads, failed = _tables()
    forgotten = (downloads if table == "downloads" else failed).clear()
    what = "downloaded track" if table == "downloads" else "failed download"
    return jsonify(
        message=f"Forgot {plural(forgotten, what)}."
        if forgotten
        else "Nothing to forget."
    )
