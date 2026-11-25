from .client import Client, new_session
from .deezer import DeezerClient
from .downloadable import BasicDownloadable, Downloadable
from .qobuz import QobuzClient
from .soundcloud import SoundcloudClient
from .spotify import SpotifyClient
from .tidal import TidalClient

# The clients by source, in the order the sources are listed.
CLIENTS = {
    c.source: c
    for c in (QobuzClient, TidalClient, DeezerClient, SoundcloudClient, SpotifyClient)
}

__all__ = [
    "CLIENTS",
    "BasicDownloadable",
    "Client",
    "DeezerClient",
    "Downloadable",
    "QobuzClient",
    "SoundcloudClient",
    "SpotifyClient",
    "TidalClient",
    "new_session",
]
