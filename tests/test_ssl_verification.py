import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from streamrip.client import new_session
from streamrip.client.qobuz import QobuzSpoofer
from streamrip.media.playlist import PendingLastfmPlaylist
from streamrip.rip.cli import coro, latest_streamrip_version, rip
from streamrip.utils import ssl_utils
from streamrip.utils.ssl_utils import get_aiohttp_connector_kwargs, print_ssl_error_help


def test_no_verification():
    assert get_aiohttp_connector_kwargs(verify_ssl=False) == {"ssl": False}


def test_verification_uses_certifi_when_installed(monkeypatch):
    certifi = MagicMock()
    certifi.where.return_value = "/ca.pem"
    monkeypatch.setattr(ssl_utils, "certifi", certifi)
    with patch("ssl.create_default_context") as create:
        assert get_aiohttp_connector_kwargs() == {"ssl": create.return_value}
    create.assert_called_once_with(cafile="/ca.pem")


def test_verification_without_certifi_uses_system_certificates(monkeypatch):
    monkeypatch.setattr(ssl_utils, "certifi", None)
    assert get_aiohttp_connector_kwargs() == {"ssl": True}


async def test_new_session_follows_verify_ssl():
    with patch(
        "streamrip.client.client.get_aiohttp_connector_kwargs",
        return_value={"ssl": False},
    ) as kwargs:
        async with new_session(verify_ssl=False):
            pass
    kwargs.assert_called_once_with(verify_ssl=False)


async def test_qobuz_spoofer_follows_verify_ssl():
    # It took verify_ssl and then verified anyway.
    with patch("streamrip.client.qobuz.new_session", return_value=AsyncMock()) as make:
        async with QobuzSpoofer(verify_ssl=False):
            pass
    make.assert_called_once_with(verify_ssl=False)


async def test_lastfm_session_follows_verify_ssl():
    config = MagicMock()
    config.session.downloads.verify_ssl = False
    playlist = PendingLastfmPlaylist("url", MagicMock(), None, config, MagicMock())
    with patch(
        "streamrip.media.playlist.new_session", side_effect=RuntimeError("stop")
    ) as make:
        with pytest.raises(RuntimeError):
            await playlist._parse_lastfm_playlist("https://www.last.fm/x")
    make.assert_called_once_with(verify_ssl=False)


async def test_update_check_follows_verify_ssl_and_is_never_fatal():
    with patch(
        "streamrip.rip.cli.new_session", side_effect=RuntimeError("offline")
    ) as make:
        _, notes, _ = await latest_streamrip_version(verify_ssl=False)
    assert notes is None
    assert make.call_args.kwargs["verify_ssl"] is False


def test_cli_option_registered():
    assert any(getattr(p, "name", "") == "no_ssl_verify" for p in rip.params)


def test_certificate_error_in_any_command_prints_help():
    # Only `url` and `file` used to catch it; the others crashed.
    error = aiohttp.ClientConnectorCertificateError(
        MagicMock(), ssl.SSLCertVerificationError("self-signed certificate")
    )

    @coro
    async def command():
        raise error

    with patch("streamrip.rip.cli.print_ssl_error_help") as help:
        command()
    help.assert_called_once()


def test_help_exits_with_an_error():
    with patch("sys.stdout"), patch("sys.exit") as exit:
        print_ssl_error_help()
    exit.assert_called_once_with(1)
