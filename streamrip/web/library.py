"""The Library page: what streamrip has downloaded and what failed, with the
retry and the clearing of `streamrip repair` and `streamrip database`."""

from itertools import starmap

from flask import Blueprint, Response, flash, redirect, render_template, url_for

from .. import db
from ..console import plural
from .common import get_config, get_runner, sent, start

library_pages = Blueprint("library", __name__, url_prefix="/library")

SHOWN = 300  # rows of a table the page lists


def _tables() -> tuple[db.Downloads, db.Failed]:
    c = get_config().session.database
    return db.Downloads(c.downloads_path), db.Failed(c.failed_downloads_path)


@library_pages.route("")
def page() -> str:
    downloads, failed = _tables()
    return render_template(
        "library.html",
        # Counted and limited by sqlite: a library may have many thousands.
        downloaded=downloads.count(),
        recent=list(starmap(db.split_download_key, downloads.recent(SHOWN))),
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
def clear(table: str):
    """Forget what was downloaded (it is downloaded again), or the failures."""
    downloads, failed = _tables()
    forgotten = (downloads if table == "downloads" else failed).clear()
    what = "downloaded track" if table == "downloads" else "failed download"
    flash(f"Forgot {plural(forgotten, what)}." if forgotten else "Nothing to forget.")
    return redirect(url_for("library.page"))
