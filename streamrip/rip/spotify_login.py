"""Log in to Spotify: catch the redirect of the browser login on a local port.

The login is OAuth with PKCE. After the user has allowed streamrip in their
browser, Spotify sends the browser to the redirect URI, which is a port on this
computer; this catches that one request. The server and its checks follow the
CLI login of Music-Sync (https://github.com/Stensel8/Music-Sync,
musicsync/oauth.py), and the loopback redirect is what spotDL does as well.

Where the browser is on another machine than streamrip (over SSH, say), the
redirect cannot arrive, and the user pastes the address they ended up on.
"""

import asyncio
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>streamrip</title>"
    b"<body style='font-family:sans-serif;text-align:center;margin-top:4rem'>"
    b"<h2>Done</h2><p>You can close this tab and return to the terminal.</p></body>"
)
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Seconds to wait for the user to finish logging in.
LOGIN_TIMEOUT = 300


class SpotifyLoginError(Exception):
    """The browser login did not give a code."""


class _CallbackServer(HTTPServer):
    """Waits for the single redirect the browser makes back after the login."""

    def __init__(self, address: tuple[str, int], path: str):
        super().__init__(address, _CallbackHandler)
        self.path = path
        self.params: dict[str, str] = {}
        # handle_request() then returns after a second, so the caller can watch
        # its deadline.
        self.timeout = 1


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Take the query of the redirect, and tell the user they can go back."""
        server = cast(_CallbackServer, self.server)
        url = urlsplit(self.path)
        if url.path != server.path:
            self.send_error(404)  # for instance the browser asking for /favicon.ico
            return
        server.params = {key: values[0] for key, values in parse_qs(url.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_DONE_PAGE)))
        self.end_headers()
        self.wfile.write(_DONE_PAGE)

    def log_message(self, format: str, *args: Any):
        """Stay quiet: the terminal belongs to streamrip."""


def check_redirect(params: dict[str, str], expected_state: str) -> str:
    """The code in the redirect's query; raises SpotifyLoginError if it is no good."""
    if "error" in params:
        raise SpotifyLoginError(f"Spotify refused the login: {params['error']}")
    if params.get("state") != expected_state:
        raise SpotifyLoginError(
            "The login does not belong to this attempt (state mismatch). "
            "Please try again."
        )
    if not params.get("code"):
        raise SpotifyLoginError("Spotify sent no authorization code.")
    return params["code"]


def code_from_redirect_url(address: str, expected_state: str) -> str:
    """The code in a redirect URL the user pasted."""
    query = urlsplit(address.strip()).query
    params = {key: values[0] for key, values in parse_qs(query).items()}
    if not params:
        raise SpotifyLoginError(
            "That address has no login in it. Paste the whole address of the page "
            "you ended up on, starting with the redirect URI."
        )
    return check_redirect(params, expected_state)


def _wait_for_redirect(redirect_uri: str, timeout: float) -> dict[str, str]:
    """Listen at the redirect URI until the browser comes, or the time is up."""
    target = urlsplit(redirect_uri)
    host = target.hostname or ""
    if host not in _LOOPBACK:
        raise SpotifyLoginError(
            f"streamrip can only catch a login on this computer: the redirect URI "
            f"should be http://127.0.0.1:<port>/..., not {redirect_uri}"
        )
    try:
        server = _CallbackServer((host, target.port or 80), target.path or "/")
    except OSError as e:
        raise SpotifyLoginError(
            f"Cannot listen on {host}:{target.port} ({e}). Something else uses "
            "that port: stop it, or choose the other way to log in."
        ) from e
    with server:  # closes the socket on the way out
        deadline = time.monotonic() + timeout
        while not server.params and time.monotonic() < deadline:
            server.handle_request()
    if not server.params:
        raise SpotifyLoginError("Timed out waiting for the login to finish.")
    return server.params


async def capture_code(redirect_uri: str, expected_state: str) -> str:
    """Wait for the browser to come back to redirect_uri, and return the code."""
    params = await asyncio.to_thread(_wait_for_redirect, redirect_uri, LOGIN_TIMEOUT)
    return check_redirect(params, expected_state)
