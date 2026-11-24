"""The service-neutral shapes the playlist tools work with.

A track is streamrip's `MatchTrack`, the same one a CSV list and a Spotify
download are matched with, so a list read here can be downloaded as it is.
"""

from dataclasses import dataclass
from typing import Literal

from ..client.audio_match import MatchTrack, normalize_isrc

Track = MatchTrack

__all__ = ["Match", "PlaylistInfo", "Track", "normalize_isrc"]


@dataclass(slots=True)
class PlaylistInfo:
    id: str
    name: str
    track_count: int | None = None
    # Spotify only shows the contents of playlists you own or collaborate on.
    readable: bool = True


@dataclass(slots=True)
class Match:
    """A track found on a service, and how it was found."""

    track: Track
    score: float
    # "playlist": already in the target playlist, no lookup needed.
    method: Literal["id", "isrc", "search", "playlist"]
    wanted: Track | None = None  # the track it was looked up for
