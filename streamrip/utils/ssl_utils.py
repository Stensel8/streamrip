"""Certificate checking for HTTPS connections."""

import functools
import ssl
import sys

lazy import certifi


def get_aiohttp_connector_kwargs(verify_ssl: bool = True) -> dict:
    """Arguments for aiohttp.TCPConnector: check certificates against
    certifi's bundle, or not at all.
    """
    if not verify_ssl:
        return {"ssl": False}
    return {"ssl": _certifi_context()}


@functools.cache
def _certifi_context() -> ssl.SSLContext:
    """One context for every session: each new one parses the bundle again."""
    return ssl.create_default_context(cafile=certifi.where())


def print_ssl_error_help():
    """Print helpful error message when SSL verification fails."""
    print("\nError: Cannot verify SSL certificate.")
    print("Options:")
    print("  1. Run again with the --no-ssl-verify flag (less secure)")
    print(
        '     Example: streamrip --no-ssl-verify url "https://tidal.com/browse/playlist/..."'
    )
    print()
    print("  2. Update streamrip: the certificates it trusts (certifi) come with it")
    sys.exit(1)
