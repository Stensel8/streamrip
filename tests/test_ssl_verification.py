import ssl
from unittest.mock import MagicMock, patch

import aiohttp
import certifi
import pytest

from streamrip.client import new_session
from streamrip.client.qobuz import QobuzSpoofer
from streamrip.media.playlist import PendingLastfmPlaylist
from streamrip.rip.cli import coro, latest_streamrip_version
from streamrip.utils.ssl_utils import get_aiohttp_connector_kwargs


def test_no_verification():
    assert get_aiohttp_connector_kwargs(verify_ssl=False) == {"ssl": False}


def test_verification_uses_the_certifi_bundle():
    context = get_aiohttp_connector_kwargs()["ssl"]
    certifi_only = ssl.create_default_context(cafile=certifi.where())
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats() == certifi_only.cert_store_stats()


@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_new_session_follows_verify_ssl(verify_ssl):
    async with new_session(verify_ssl=verify_ssl) as session:
        # aiohttp keeps what a connection is checked with here.
        assert isinstance(session.connector._ssl, ssl.SSLContext) is verify_ssl


async def test_qobuz_spoofer_follows_verify_ssl():
    # It took verify_ssl and then verified anyway.
    async with QobuzSpoofer(verify_ssl=False) as spoofer:
        assert spoofer.session.connector._ssl is False


async def test_lastfm_session_follows_verify_ssl():
    config = MagicMock()
    config.session.downloads.verify_ssl = False
    playlist = PendingLastfmPlaylist("url", MagicMock(), None, config, MagicMock())
    with (
        patch(
            "streamrip.media.playlist.new_session", side_effect=RuntimeError("stop")
        ) as make,
        pytest.raises(RuntimeError),
    ):
        await playlist._parse_lastfm_playlist("https://www.last.fm/x")
    make.assert_called_once_with(verify_ssl=False)


async def test_update_check_follows_verify_ssl_and_is_never_fatal():
    with patch(
        "streamrip.rip.cli.new_session", side_effect=RuntimeError("offline")
    ) as make:
        _, notes, _ = await latest_streamrip_version(verify_ssl=False)
    assert notes is None
    assert make.call_args.kwargs["verify_ssl"] is False


def test_certificate_error_in_any_command_prints_help(capsys):
    # Only `url` and `file` used to catch it; the others crashed.
    error = aiohttp.ClientConnectorCertificateError(
        MagicMock(), ssl.SSLCertVerificationError("self-signed certificate")
    )

    @coro
    async def command():
        raise error

    with pytest.raises(SystemExit) as ended:
        command()
    assert ended.value.code == 1
    assert "--no-ssl-verify" in capsys.readouterr().out
