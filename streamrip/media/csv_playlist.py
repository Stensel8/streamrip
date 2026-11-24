"""A list of tracks in a CSV file, found by searching a source.

`streamrip playlist export` saves a Spotify or Tidal playlist as a CSV, and so
do many other tools (Exportify, TuneMyMusic, ...). sync/csvio.py reads the file;
each row is then searched for on the source the user picks, the way a last.fm
playlist is (see `playlist.searched_playlist`).
"""

import logging
from dataclasses import dataclass

from ..sync.csvio import read_tracks
from .media import Pending
from .playlist import Playlist, searched_playlist
lazy from ..client import Client
lazy from ..client.audio_match import MatchTrack
lazy from ..config import Config
lazy from ..db import Database

logger = logging.getLogger("streamrip")

__all__ = ["PendingCsvPlaylist", "read_tracks"]


@dataclass(slots=True)
class PendingCsvPlaylist(Pending):
    """A CSV list, to be searched on a source (and on others, in turn, for what it lacks)."""

    name: str
    tracks: list[MatchTrack]
    client: Client
    fallbacks: list[Client]
    config: Config
    db: Database

    async def resolve(self) -> Playlist | None:
        """Search every track and make a playlist of the ones that were found."""
        return await searched_playlist(
            self.name,
            self.tracks,
            self.client,
            self.fallbacks,
            self.config,
            self.db,
        )
