import http.client

from . import converter, db, exceptions, media, metadata
from .config import Config

# Prevent failures on CDNs (like Qobuz/Akamai) that send >100 response headers
http.client._MAXHEADERS = 1000  # type: ignore[attr-defined]

__all__ = ["Config", "converter", "db", "exceptions", "media", "metadata"]
__version__ = "2.4.5"
