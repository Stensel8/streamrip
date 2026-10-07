"""The browser login: the redirect caught on a local port, or pasted by hand."""

import asyncio
import socket
import urllib.error
import urllib.request

import pytest

from streamrip.rip import spotify_login
from streamrip.rip.spotify_login import (
    SpotifyLoginError,
    capture_code,
    check_redirect,
    code_from_redirect_url,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_redirect_with_the_right_state_gives_its_code():
    assert check_redirect({"code": "abc", "state": "s1"}, "s1") == "abc"


def test_a_refused_login_says_so():
    with pytest.raises(SpotifyLoginError, match="access_denied"):
        check_redirect({"error": "access_denied", "state": "s1"}, "s1")


def test_a_redirect_from_another_login_is_refused():
    with pytest.raises(SpotifyLoginError, match="state mismatch"):
        check_redirect({"code": "abc", "state": "someone-elses"}, "s1")


def test_a_redirect_without_a_code_is_refused():
    with pytest.raises(SpotifyLoginError, match="no authorization code"):
        check_redirect({"state": "s1"}, "s1")


def test_a_pasted_address_gives_its_code():
    address = " http://127.0.0.1:9900/callback?code=abc%2F123&state=s1 "

    assert code_from_redirect_url(address, "s1") == "abc/123"


def test_a_pasted_address_without_a_login_in_it_is_refused():
    with pytest.raises(SpotifyLoginError, match="no login in it"):
        code_from_redirect_url("http://127.0.0.1:9900/callback", "s1")
    with pytest.raises(SpotifyLoginError, match="no login in it"):
        code_from_redirect_url("", "s1")


async def browse(url: str):
    """What the browser does after the login: GET the redirect URI."""
    # The server is started in a thread by capture_code: wait for its port.
    for _ in range(50):
        try:
            return await asyncio.to_thread(
                lambda: urllib.request.urlopen(url, timeout=5).read()
            )
        except urllib.error.HTTPError:
            raise
        except OSError:
            await asyncio.sleep(0.1)
    raise AssertionError("the login server never came up")


async def test_the_code_comes_back_through_the_local_port():
    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    code, page = await asyncio.gather(
        capture_code(redirect_uri, "s1"),
        browse(f"{redirect_uri}?code=the-code&state=s1"),
    )

    assert code == "the-code"
    assert b"You can close this tab" in page


async def test_a_login_for_another_attempt_is_not_accepted():
    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    with pytest.raises(SpotifyLoginError, match="state mismatch"):
        await asyncio.gather(
            capture_code(redirect_uri, "s1"),
            browse(f"{redirect_uri}?code=the-code&state=other"),
        )


async def test_other_pages_do_not_end_the_wait():
    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    async def favicon_then_login():
        with pytest.raises(urllib.error.HTTPError, match="404"):
            await browse(f"http://127.0.0.1:{port}/favicon.ico")
        return await browse(f"{redirect_uri}?code=ok&state=s1")

    code, _ = await asyncio.gather(
        capture_code(redirect_uri, "s1"), favicon_then_login()
    )

    assert code == "ok"


async def test_waiting_ends_when_the_time_is_up(monkeypatch):
    monkeypatch.setattr(spotify_login, "LOGIN_TIMEOUT", 0.2)

    with pytest.raises(SpotifyLoginError, match="Timed out"):
        await capture_code(f"http://127.0.0.1:{free_port()}/callback", "s1")


async def test_a_redirect_uri_that_is_not_on_this_computer_cannot_be_caught():
    with pytest.raises(SpotifyLoginError, match="only catch a login on this computer"):
        await capture_code("https://example.com/callback", "s1")


async def test_a_port_in_use_is_reported():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]

        with pytest.raises(SpotifyLoginError, match="Cannot listen"):
            await capture_code(f"http://127.0.0.1:{port}/callback", "s1")
