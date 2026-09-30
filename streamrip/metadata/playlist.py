import logging
from dataclasses import dataclass

from .album import AlbumMetadata
from .track import TrackMetadata
from .util import safe_get, typed

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class PlaylistMetadata:
    """A playlist's name and its tracks, resolved or as bare ids."""

    name: str
    tracks: list[TrackMetadata] | list[str]

    @classmethod
    def from_qobuz(cls, resp: dict):
        """Build playlist metadata from a Qobuz API playlist response."""
        logger.debug(resp)
        name = typed(resp["name"], str)
        tracks = []
        items = safe_get(resp, "tracks", "items", default=[]) or []

        track_ids = [str(i) for i in resp.get("track_ids") or []]
        if len(track_ids) > len(items):
            # The inline track list is capped at one page (500 tracks) and has
            # been empty since Qobuz's July 2026 API change, while track_ids
            # lists every track. Each id is resolved by PendingPlaylistTrack.
            return cls(name, track_ids)

        for i, track in enumerate(items):
            try:
                meta = TrackMetadata.from_qobuz(
                    AlbumMetadata.from_qobuz(track["album"]),
                    track,
                )
            except Exception as e:
                # One malformed entry should not sink the whole playlist.
                logger.error(f"Error reading track {i + 1} in playlist {name}: {e}")
                continue
            if meta is None:
                logger.error(
                    f"Track {i + 1} in playlist {name} not available for stream"
                )
                continue
            tracks.append(meta)

        return cls(name, tracks)

    @classmethod
    def from_soundcloud(cls, resp: dict):
        """Convert a (modified) soundcloud API response to PlaylistMetadata.

        Args:
        ----
            resp (dict): The response, except there should not be any partially resolved items
            in the playlist.

            e.g. If soundcloud only returns the full metadata of 5 of them, the rest of the
            elements in resp['tracks'] should be replaced with their full metadata.

        Returns:
        -------
            PlaylistMetadata object.
        """
        name = typed(resp["title"], str)
        tracks = [
            TrackMetadata.from_soundcloud(AlbumMetadata.from_soundcloud(track), track)
            for track in resp["tracks"]
        ]
        return cls(name, tracks)

    @classmethod
    def from_deezer(cls, resp: dict):
        """Build playlist metadata from a Deezer API playlist response.

        Tracks the user uploaded to Deezer themselves are left out.
        """
        name = typed(resp["title"], str)
        ids = [str(track["id"]) for track in resp["tracks"]]
        # Tracks you uploaded to Deezer yourself have negative ids and no album
        # to tag them with; they're your own files anyway (upstream PR #832).
        tracks = [i for i in ids if not i.startswith("-")]
        if len(tracks) < len(ids):
            logger.info(
                f"{name}: skipping {len(ids) - len(tracks)} track(s) you uploaded "
                "to Deezer yourself"
            )
        return cls(name, tracks)

    @classmethod
    def from_tidal(cls, resp: dict):
        """Build playlist metadata from a Tidal API playlist response."""
        name = typed(resp["title"], str)
        tracks = [str(track["id"]) for track in resp["tracks"]]
        return cls(name, tracks)

    def ids(self) -> list[str]:
        """Return the track ids, resolving TrackMetadata entries to their id."""
        if len(self.tracks) == 0:
            return []
        if isinstance(self.tracks[0], str):
            return self.tracks  # type: ignore

        return [track.info.id for track in self.tracks]  # type: ignore

    @classmethod
    def from_resp(cls, resp: dict, source: str):
        """Dispatch to the from_* builder matching source."""
        if source == "qobuz":
            return cls.from_qobuz(resp)
        elif source == "soundcloud":
            return cls.from_soundcloud(resp)
        elif source == "deezer":
            return cls.from_deezer(resp)
        elif source == "tidal":
            return cls.from_tidal(resp)
        else:
            raise NotImplementedError(source)
