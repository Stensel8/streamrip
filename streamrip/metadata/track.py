from __future__ import annotations

import logging
from dataclasses import dataclass

from .album import AlbumMetadata
from .util import deezer_artists, safe_get

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class TrackInfo:
    id: str
    explicit: bool = False


@dataclass(slots=True)
class TrackMetadata:
    info: TrackInfo

    title: str
    album: AlbumMetadata
    artist: str
    tracknumber: int
    discnumber: int
    composer: str | None
    isrc: str | None = None
    lyrics: str | None = ""
    # Individual artist names, when the source distinguishes them (Tidal,
    # Deezer). `artist` above stays a single display string (joined with
    # ", ") for filenames and templates; the tagger writes this list as a
    # real multi-valued ARTIST tag instead of baking the join into one
    # string, which is what let players mis-split "A, B" back apart.
    artists: list[str] | None = None

    @classmethod
    def from_qobuz(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata | None:
        if not resp.get("streamable", False):
            return None
        title = resp["title"].strip()
        version, work = resp.get("version"), resp.get("work")
        if version and version not in title:
            title = f"{title} ({version})"
        if work and work not in title:
            title = f"{work}: {title}"
        return cls(
            TrackInfo(str(resp["id"]), bool(resp.get("parental_warning"))),
            title,
            album,
            # "performer" is missing on some tracks (upstream #668); fall back
            # to the album artist rather than failing the whole track.
            safe_get(resp, "performer", "name")
            or safe_get(resp, "album", "artist", "name")
            or album.albumartist,
            resp.get("track_number", 1),
            resp.get("media_number", 1),
            safe_get(resp, "composer", "name"),
            isrc=resp.get("isrc"),
        )

    @classmethod
    def from_deezer(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata:
        artists = deezer_artists(resp)
        return cls(
            TrackInfo(str(resp["id"]), bool(resp.get("explicit_lyrics"))),
            resp["title"],
            album,
            ", ".join(artists),
            resp["track_position"],
            resp["disk_number"],
            None,
            isrc=resp.get("isrc"),
            lyrics=resp.get("lyrics", ""),
            artists=artists,
        )

    @classmethod
    def from_soundcloud(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata:
        publisher = resp.get("publisher_metadata") or {}
        return cls(
            TrackInfo(str(resp["id"]), bool(publisher.get("explicit"))),
            resp["title"].strip(),
            album,
            resp["user"]["username"],
            1,
            1,
            None,
            isrc=publisher.get("isrc"),
        )

    @classmethod
    def from_tidal(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata:
        title = resp["title"].strip()
        if version := resp.get("version"):
            title = f"{title} ({version})"
        artists = [a["name"] for a in resp.get("artists") or []] or [
            resp["artist"]["name"]
        ]
        return cls(
            TrackInfo(str(resp["id"]), bool(resp.get("explicit"))),
            title,
            album,
            ", ".join(artists),
            resp.get("trackNumber", 1),
            resp.get("volumeNumber", 1),
            None,
            isrc=resp.get("isrc"),
            lyrics=resp.get("lyrics", ""),
            artists=artists,
        )

    @classmethod
    def from_resp(cls, album: AlbumMetadata, source, resp) -> TrackMetadata | None:
        if source == "qobuz":
            return cls.from_qobuz(album, resp)
        if source == "tidal":
            return cls.from_tidal(album, resp)
        if source == "soundcloud":
            return cls.from_soundcloud(album, resp)
        if source == "deezer":
            return cls.from_deezer(album, resp)
        raise Exception(f"Invalid source {source}")

    def format_track_path(self, format_string: str) -> str:
        # Available keys: "id", "tracknumber", "discnumber", "artist", "album",
        # "albumartist", "composer", "title", "explicit", "albumcomposer"
        none_text = "Unknown"
        info = {
            "id": self.info.id,
            "title": self.title,
            "tracknumber": self.tracknumber,
            "discnumber": self.discnumber,
            "artist": self.artist,
            "album": self.album.album,
            # Alias requested upstream (PR #826).
            "albumtitle": self.album.album,
            "albumartist": self.album.albumartist,
            "albumcomposer": self.album.albumcomposer or none_text,
            "composer": self.composer or none_text,
            "explicit": " (Explicit) " if self.info.explicit else "",
        }
        return format_string.format(**info)
