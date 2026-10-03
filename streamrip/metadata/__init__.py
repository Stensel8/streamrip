"""Manages the information that will be embeded in the audio file."""

from . import util
from .album import AlbumInfo, AlbumMetadata
from .artist import ArtistMetadata
from .covers import Covers
from .label import LabelMetadata
from .playlist import PlaylistMetadata
from .search_results import SearchResults, Summary
from .tagger import tag_file
from .track import TrackInfo, TrackMetadata

__all__ = [
    "AlbumInfo",
    "AlbumMetadata",
    "ArtistMetadata",
    "Covers",
    "LabelMetadata",
    "PlaylistMetadata",
    "SearchResults",
    "Summary",
    "TrackInfo",
    "TrackMetadata",
    "tag_file",
    "util",
]
