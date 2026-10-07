from .client import Client, new_session
from .deezer import DeezerClient
from .downloadable import BasicDownloadable, Downloadable
from .qobuz import QobuzClient
from .soundcloud import SoundcloudClient
from .spotify import SpotifyClient
from .tidal import TidalClient

__all__ = [
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
