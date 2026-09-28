"""Certificate checking for HTTPS connections."""

import logging
import ssl
import sys

logger = logging.getLogger("streamrip")

try:
    import certifi
except ImportError:
    logger.debug("certifi not found, falling back to system certificates")
    certifi = None


def get_aiohttp_connector_kwargs(verify_ssl: bool = True) -> dict:
    """Arguments for aiohttp.TCPConnector: check certificates against
    certifi's bundle when it's installed, else the system's, or not at all.
    """
    if not verify_ssl:
        return {"ssl": False}
    if certifi is not None:
        return {"ssl": ssl.create_default_context(cafile=certifi.where())}
    return {"ssl": True}


def print_ssl_error_help():
    """Print helpful error message when SSL verification fails."""
    print("\nError: Cannot verify SSL certificate.")
    print("Options:")
    print("  1. Run again with the --no-ssl-verify flag (less secure)")
    print(
        '     Example: streamrip --no-ssl-verify url "https://tidal.com/browse/playlist/..."'
    )
    print()
    print("  2. Install certifi for better certificate handling:")
    print("     pip install certifi")
    print()
    print("  3. Update your certificates:")
    print("     pip install --upgrade certifi")
    sys.exit(1)
