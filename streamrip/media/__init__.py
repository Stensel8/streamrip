from .album import Album, PendingAlbum
from .artist import Artist, PendingArtist
from .artwork import remove_artwork_tempdirs
from .label import Label, PendingLabel
from .media import Media, Pending
from .playlist import (
    PendingLastfmPlaylist,
    PendingPlaylist,
    PendingPlaylistTrack,
    Playlist,
)
from .track import PendingSingle, PendingTrack, Track

PENDING_TYPES: dict[str, type[Pending]] = {
    "track": PendingSingle,
    "album": PendingAlbum,
    "playlist": PendingPlaylist,
    "artist": PendingArtist,
    "label": PendingLabel,
}


def pending_item(media_type: str, item_id: str, client, config, db) -> Pending:
    """The Pending for an item of media_type, by its id."""
    if media_type not in PENDING_TYPES:
        raise NotImplementedError(media_type)
    return PENDING_TYPES[media_type](item_id, client, config, db)


__all__ = [
    "Album",
    "Artist",
    "Label",
    "Media",
    "Pending",
    "PendingAlbum",
    "PendingArtist",
    "PendingLabel",
    "PendingLastfmPlaylist",
    "PendingPlaylist",
    "PendingPlaylistTrack",
    "PendingSingle",
    "PendingTrack",
    "Playlist",
    "Track",
    "pending_item",
    "remove_artwork_tempdirs",
]
