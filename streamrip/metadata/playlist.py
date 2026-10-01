import logging
from dataclasses import dataclass

from .util import safe_get, typed

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class PlaylistMetadata:
    """A playlist's name and the ids of its tracks.

    Each track's own metadata is fetched when it's downloaded
    (PendingPlaylistTrack), so the playlist only needs the ids.
    """

    name: str
    ids: list[str]

    @classmethod
    def from_qobuz(cls, resp: dict):
        """Build playlist metadata from a Qobuz API playlist response."""
        logger.debug(resp)
        name = typed(resp["name"], str)
        items = safe_get(resp, "tracks", "items", default=[]) or []

        track_ids = [str(i) for i in resp.get("track_ids") or []]
        if len(track_ids) > len(items):
            # The inline track list is capped at one page (500 tracks) and has
            # been empty since Qobuz's July 2026 API change, while track_ids
            # lists every track.
            return cls(name, track_ids)

        ids = [str(track["id"]) for track in items if track.get("streamable")]
        if len(ids) < len(items):
            logger.error(
                f"{len(items) - len(ids)} track(s) in playlist {name} not "
                "available for stream"
            )
        return cls(name, ids)

    @classmethod
    def from_soundcloud(cls, resp: dict):
        """Build playlist metadata from SoundcloudClient's playlist response,
        whose tracks all carry their custom ids.
        """
        return cls(typed(resp["title"], str), [str(t["id"]) for t in resp["tracks"]])

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
        return cls(typed(resp["title"], str), [str(t["id"]) for t in resp["tracks"]])

    @classmethod
    def from_resp(cls, resp: dict, source: str):
        """Dispatch to the from_* builder matching source."""
        if source == "qobuz":
            return cls.from_qobuz(resp)
        if source == "soundcloud":
            return cls.from_soundcloud(resp)
        if source == "deezer":
            return cls.from_deezer(resp)
        if source == "tidal":
            return cls.from_tidal(resp)
        raise NotImplementedError(source)
