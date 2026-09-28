from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from ..filepath_utils import clean_filename, clean_filepath
from .covers import Covers
from .util import deezer_artists, get_quality_id, safe_get, tidal_quality_id, typed

PHON_COPYRIGHT = "\u2117"
COPYRIGHT = "\u00a9"

logger = logging.getLogger("streamrip")


genre_clean = re.compile(r"([^\u2192\/]+)")


def _year(date: str | None) -> str:
    return date[:4] if date else "Unknown"


@dataclass(slots=True)
class AlbumInfo:
    id: str
    quality: int
    container: str
    label: Optional[str] = None
    explicit: bool = False
    sampling_rate: int | float | None = None
    bit_depth: int | None = None
    booklets: list[dict] | None = None


@dataclass(slots=True)
class AlbumMetadata:
    info: AlbumInfo
    album: str
    albumartist: str
    year: str
    genre: list[str]
    covers: Covers
    tracktotal: int
    disctotal: int = 1
    albumcomposer: str | None = None
    # Set on the tracks of a playlist downloaded as one album.
    compilation: str | None = None
    copyright: str | None = None
    date: str | None = None
    description: str | None = None
    # Edition name, e.g. "Deluxe Edition". Only some sources provide one.
    version: str | None = None

    def get_genres(self) -> str:
        return ", ".join(self.genre)

    def get_copyright(self) -> str | None:
        if self.copyright is None:
            return None
        # Add special chars
        _copyright = re.sub(r"(?i)\(P\)", PHON_COPYRIGHT, self.copyright)
        _copyright = re.sub(r"(?i)\(C\)", COPYRIGHT, _copyright)
        return _copyright

    def format_folder_path(self, formatter: str) -> str:
        # Available keys: "albumartist", "title", "year", "bit_depth", "sampling_rate",
        # "id", "albumcomposer", "container", "tracktotal", and "version".
        #
        # Two different editions of the same album can otherwise render to the
        # same folder and get merged together -- "tracktotal" and "version"
        # give a readable way to tell them apart without resorting to "id".

        none_str = "Unknown"
        info: dict[str, str | int | float] = {
            "albumartist": clean_filename(self.albumartist),
            "albumcomposer": clean_filename(self.albumcomposer or "") or none_str,
            "bit_depth": self.info.bit_depth or none_str,
            "id": self.info.id,
            "sampling_rate": self.info.sampling_rate or none_str,
            "title": clean_filename(self.album),
            "year": self.year,
            "container": self.info.container,
            "tracktotal": self.tracktotal,
            "version": clean_filename(self.version or "") or none_str,
        }

        return clean_filepath(formatter.format(**info))

    @classmethod
    def from_qobuz(cls, resp: dict) -> AlbumMetadata:
        album = (resp.get("title") or "Unknown Album").strip()
        version = (resp.get("version") or "").strip() or None
        # The edition is part of what the album *is* -- a standard and a deluxe
        # release otherwise share a title, so they tag identically and collide
        # in the same download folder. Fold it into the title (unless the title
        # already says it) and keep it in `version` for tagging as well.
        if version and version.lower() not in album.lower():
            album = f"{album} ({version})"
        genre = safe_get(resp, "genre", "name")
        date = resp.get("release_date_original") or resp.get("release_date")
        if artists := resp.get("artists"):
            albumartist = ", ".join(a["name"] for a in artists)
        else:
            albumartist = safe_get(resp, "artist", "name") or "Unknown Artist"
        label = resp.get("label")
        if isinstance(label, dict):
            label = label["name"]
        bit_depth = typed(resp.get("maximum_bit_depth", -1), int)
        sampling_rate = typed(resp.get("maximum_sampling_rate", -1.0), int | float)

        info = AlbumInfo(
            id=str(resp.get("qobuz_id")),
            quality=get_quality_id(bit_depth, sampling_rate),
            container="FLAC" if sampling_rate and bit_depth else "MP3",
            label=label or "",
            explicit=bool(resp.get("parental_warning")),
            sampling_rate=sampling_rate,
            bit_depth=bit_depth,
            booklets=resp.get("goodies") or None,
        )
        return cls(
            info,
            album,
            albumartist,
            _year(date),
            genre=list(set(genre_clean.findall(genre)))
            if isinstance(genre, str)
            else [],
            covers=Covers.from_qobuz(resp),
            tracktotal=resp.get("tracks_count", 1),
            # "media_count" is authoritative and is present even when the album
            # object is abbreviated. The track list is only embedded in a full
            # album response, so deriving the disc count from it alone collapses
            # to 1 for any album reached via a track (e.g. a single-track URL or
            # `streamrip repair`), which then loses the "Disc N" subfolder.
            disctotal=resp.get("media_count")
            or max(
                track.get("media_number", 1)
                for track in safe_get(resp, "tracks", "items", default=[{}])  # type: ignore
            )
            or 1,
            albumcomposer=safe_get(resp, "composer", "name", default=""),
            copyright=resp.get("copyright", ""),
            date=date,
            description=resp.get("description") or "",
            version=version,
        )

    @classmethod
    def from_deezer(cls, resp: dict) -> AlbumMetadata:
        date = resp.get("release_date")
        info = AlbumInfo(
            id=str(resp["id"]),
            quality=2,
            container="FLAC",
            label=resp.get("label"),
            explicit=bool(resp.get("parental_warning") or resp.get("explicit_lyrics")),
            sampling_rate=44.1,
            bit_depth=16,
        )
        return cls(
            info,
            resp.get("title") or "Unknown Album",
            ", ".join(deezer_artists(resp)),
            _year(date),
            genre=[
                g["name"] for g in safe_get(resp, "genres", "data", default=[]) or []
            ],
            covers=Covers.from_deezer(resp),
            tracktotal=resp.get("track_total") or resp.get("nb_tracks") or 0,
            disctotal=resp["tracks"][-1]["disk_number"] if resp["tracks"] else 1,
            date=date,
        )

    @classmethod
    def from_incomplete_deezer_track_resp(cls, resp: dict) -> AlbumMetadata:
        """Album metadata from a track whose album response has no track list."""
        album = resp["album"]
        date = album.get("release_date")
        info = AlbumInfo(
            id=str(album["id"]),
            quality=2,
            container="FLAC",
            explicit=bool(resp.get("explicit_lyrics")),
            sampling_rate=44.1,
            bit_depth=16,
        )
        return cls(
            info,
            album.get("title") or "Unknown Album",
            ", ".join(deezer_artists(resp)),
            _year(date),
            genre=[],
            covers=Covers.from_deezer(album),
            tracktotal=1,
            date=date,
        )

    @classmethod
    def from_soundcloud(cls, resp: dict) -> AlbumMetadata:
        # SoundCloud has no albums: a track stands in for its own.
        publisher = resp.get("publisher_metadata") or {}
        date = resp.get("created_at")
        info = AlbumInfo(
            id=str(resp["id"]),
            quality=0,
            container="MP3",
            label=resp.get("label_name"),
            explicit=bool(publisher.get("explicit")),
        )
        return cls(
            info,
            publisher.get("album_title") or "Unknown album",
            publisher.get("artist") or resp["user"]["username"],
            _year(date),
            genre=[resp["genre"]] if resp.get("genre") else [],
            covers=Covers.from_soundcloud(resp),
            tracktotal=1,
            copyright=publisher.get("p_line"),
            date=date,
            description=resp.get("description"),
        )

    @classmethod
    def from_tidal(cls, resp: dict) -> AlbumMetadata | None:
        """None if the album can't be streamed."""
        if not resp.get("allowStreaming", False):
            return None
        quality = tidal_quality_id(resp.get("audioQuality", "LOW"))
        lossless = quality >= 2
        # The album only says LOSSLESS. For a hi-res one the client adds what
        # its stream really is (bit depth, and sample rate in Hz).
        stream = resp.get("streamQuality") or {}
        if stream.get("bitDepth") and stream.get("sampleRate"):
            quality, lossless = 3, True
            bit_depth = stream["bitDepth"]
            khz = stream["sampleRate"] / 1000
            sampling_rate = int(khz) if khz.is_integer() else khz
        else:
            # Tidal doesn't say; this is its lossless tier.
            bit_depth = (24 if quality == 3 else 16) if lossless else None
            sampling_rate = 44.1 if lossless else None
        date = resp.get("releaseDate")
        artists = ", ".join(a["name"] for a in resp.get("artists") or [])
        info = AlbumInfo(
            id=str(resp["id"]),
            quality=quality,
            container="FLAC" if lossless else "AAC",
            explicit=bool(resp.get("explicit")),
            sampling_rate=sampling_rate,
            bit_depth=bit_depth,
        )
        return cls(
            info,
            resp.get("title") or "Unknown Album",
            artists or safe_get(resp, "artist", "name", default="Unknown Artist"),
            _year(date),
            genre=[],
            covers=Covers.from_tidal(resp) or Covers(),
            tracktotal=resp.get("numberOfTracks", 1),
            disctotal=resp.get("numberOfVolumes", 1),
            copyright=resp.get("copyright") or "",
            date=date,
        )

    @classmethod
    def from_tidal_playlist_track_resp(cls, resp: dict) -> AlbumMetadata | None:
        """Album metadata from a track response. That only carries the album's
        id, title and cover, so the rest is taken from the track itself.
        """
        return cls.from_tidal(
            resp["album"]
            | {
                "allowStreaming": resp.get("allowStreaming", False),
                "audioQuality": resp.get("audioQuality", "LOW"),
                "explicit": resp.get("explicit"),
                "releaseDate": resp.get("streamStartDate"),
                "copyright": resp.get("copyright"),
                "artists": resp.get("artists"),
                "artist": resp.get("artist"),
                "numberOfVolumes": resp.get("volumeNumber", 1),
            }
        )

    @classmethod
    def from_track_resp(cls, resp: dict, source: str) -> AlbumMetadata | None:
        if source == "qobuz":
            return cls.from_qobuz(resp["album"])
        if source == "tidal":
            return cls.from_tidal_playlist_track_resp(resp)
        if source == "soundcloud":
            return cls.from_soundcloud(resp)
        if source == "deezer":
            if "tracks" not in resp["album"]:
                return cls.from_incomplete_deezer_track_resp(resp)
            return cls.from_deezer(resp["album"])
        raise Exception("Invalid source")

    @classmethod
    def from_album_resp(cls, resp: dict, source: str) -> AlbumMetadata | None:
        if source == "qobuz":
            return cls.from_qobuz(resp)
        if source == "tidal":
            return cls.from_tidal(resp)
        if source == "soundcloud":
            return cls.from_soundcloud(resp)
        if source == "deezer":
            return cls.from_deezer(resp)
        raise Exception("Invalid source")
