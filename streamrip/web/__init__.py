"""`streamrip web`: downloads, search, the playlist tools and every login, in
a browser on this computer.

Its security: only loopback names are answered (no DNS rebinding), changes
from another website are refused (no CSRF), and the pages run only their own
scripts.
"""

import contextlib
import os
import secrets
from urllib.parse import urlsplit

import aiohttp
import click
from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException, SecurityError

from ..client.spotify import SpotifyAuthError
from ..config import APP_DIR
from ..exceptions import (
    APIError,
    AuthenticationError,
    FFmpegNotFoundError,
    MissingCredentialsError,
    NonStreamableError,
)
from ..rip.spotify_login import SpotifyLoginError
from ..sync.errors import SyncError
from ..sync.services import Services
from .accounts import accounts_pages
from .jobs import JobManager, explain
from .library import library_pages
from .runner import AccountPausedError, Runner
from .views import pages, playlist_pages
lazy from ..config import Config

# Failures whose message is for the user, in a job or a request: a service
# that refused or could not be reached, a full disk, a login that is missing.
EXPLAINED = (
    aiohttp.ClientError,
    OSError,
    SyncError,
    AuthenticationError,
    MissingCredentialsError,
    FFmpegNotFoundError,
    NonStreamableError,
    APIError,
    SpotifyLoginError,
    SpotifyAuthError,
    AccountPausedError,
    click.ClickException,  # what the command line says too: a database that cannot be made
)

# The pages use only their own scripts and styles, and nothing inline; covers
# come from the services' own servers. Should a track or playlist name ever get
# past the escaping, the browser still refuses to run it.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' https:; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def _secret_key() -> str:
    """The key that signs the session cookie: made once, kept next to the config."""
    path = APP_DIR / "web-secret-key"
    with contextlib.suppress(OSError):
        if key := path.read_text(encoding="utf-8").strip():
            return key
    key = secrets.token_hex(32)
    with contextlib.suppress(OSError):  # a folder we cannot write: it lasts the run
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key)
    return key


def _reject_cross_origin():
    """Refuse changes started by another website (CSRF). Browsers say where a
    request came from (Sec-Fetch-Site, Origin) and page scripts cannot forge
    that, so no tokens are needed."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        # "none": the user typed the address or used a bookmark.
        allowed = site in {"same-origin", "none"}
    else:
        # Browsers from before 2023 only send Origin. A request with neither
        # does not come from a web page (curl, the tests).
        origin = request.headers.get("Origin")
        allowed = origin is None or urlsplit(origin).netloc == request.host
    if not allowed:
        abort(403, "Request from another website refused.")


def _security_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _report(e: Exception) -> tuple[Response | str, int]:
    """An explained failure or an HTTP error: JSON for the page's own fetch()
    calls, else an error page with a way back."""
    if isinstance(e, HTTPException):
        status, message = e.code or 500, e.description or ""
    elif isinstance(e, EXPLAINED):
        network = isinstance(e, APIError | aiohttp.ClientError)
        status = getattr(e, "http_status", 502 if network else 400)
        message = explain(e)
    else:
        raise e
    if request.accept_mimetypes.best == "application/json":
        return jsonify(status="error", message=message), status
    return render_template("error.html", message=message, status=status), status


def _unknown_host(_e: SecurityError) -> Response:
    """A Host header that is not ours. Flask cannot build URLs for it."""
    return Response("Unknown host name.", status=400, mimetype="text/plain")


def create_app(
    config: Config,
    *,
    services: Services | None = None,
    runner: Runner | None = None,
    secret_key: str | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key or _secret_key(),
        # Only answer to our own address, so a page on another site cannot
        # reach us under its own name (DNS rebinding).
        TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"],
        # Lax still sends the cookie on the redirect back from a login.
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # a CSV of thousands of tracks is far less
    )
    app.extensions["config"] = config
    app.extensions["services"] = services or Services(config)
    app.extensions["runner"] = runner or Runner(config)
    app.extensions["jobs"] = JobManager(EXPLAINED)
    app.extensions["logins"] = {}

    app.before_request(_reject_cross_origin)
    app.after_request(_security_headers)
    app.register_error_handler(SecurityError, _unknown_host)
    for error in (*EXPLAINED, HTTPException):
        app.register_error_handler(error, _report)
    app.register_blueprint(pages)
    app.register_blueprint(accounts_pages)
    app.register_blueprint(library_pages)
    app.register_blueprint(playlist_pages)
    return app
