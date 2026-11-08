import logging
from dataclasses import dataclass

from .util import SOURCES, deezer_artists, qobuz_artists, qobuz_credited, safe_get
lazy from .album import AlbumMetadata

logger = logging.getLogger("streamrip")


def _key(note: str | None, scale: str | None) -> str | None:
    """Tidal's key and scale as one key the way TKEY spells it, "m" for minor.
    Tidal writes sharps and flats out as words.
    """
    if not note:
        return None
    note = note.replace("Sharp", "#").replace("Flat", "b")
    return f"{note}m" if (scale or "").upper() == "MINOR" else note


@dataclass(slots=True)
class TrackInfo:
    """A track's id and whether it's the explicit edition."""

    id: str
    explicit: bool = False


@dataclass(slots=True)
class TrackMetadata:
    """A track's metadata, built from one streaming source's API response."""

    info: TrackInfo

    title: str
    album: AlbumMetadata
    artist: str
    tracknumber: int
    discnumber: int
    composer: str | None
    isrc: str | None = None
    lyrics: str | None = ""
    # Individual artist names, where the source lists them. `artist` above
    # stays a single display string (joined with ", ") for filenames and
    # templates; the tagger writes this list as a real multi-valued ARTIST
    # tag instead of baking the join into one string, which players then
    # show as one artist named "A, B".
    artists: list[str] | None = None
    # Like `artists`: `composer` above is them joined.
    composers: list[str] | None = None
    lyricists: list[str] | None = None
    bpm: int | None = None
    # ReplayGain of the track: gain in dB, peak as a fraction of full scale.
    replaygain_gain: float | None = None
    replaygain_peak: float | None = None
    # The musical key as DJ software writes it: "Bb", "F#m".
    key: str | None = None

    @classmethod
    def from_qobuz(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata | None:
        """Build track metadata from a Qobuz API track response."""
        if not resp.get("streamable"):
            return None
        title = resp["title"].strip()
        version, work = resp.get("version"), resp.get("work")
        if version and version not in title:
            title = f"{title} ({version})"
        if work and work not in title:
            title = f"{work}: {title}"
        artists = qobuz_artists(resp)
        composers = qobuz_credited(resp, "composer", "composerlyricist") or [
            name for name in [safe_get(resp, "composer", "name")] if name
        ]
        gain = resp.get("audio_info") or {}
        return cls(
            TrackInfo(str(resp["id"]), bool(resp.get("parental_warning"))),
            title,
            album,
            # "performer" is missing on some tracks (upstream #668); fall back
            # to the album artist rather than failing the whole track.
            ", ".join(artists)
            or safe_get(resp, "performer", "name")
            or safe_get(resp, "album", "artist", "name")
            or album.albumartist,
            resp.get("track_number", 1),
            resp.get("media_number", 1),
            ", ".join(composers) or None,
            isrc=resp.get("isrc"),
            artists=artists or None,
            composers=composers or None,
            lyricists=qobuz_credited(resp, "lyricist", "composerlyricist") or None,
            replaygain_gain=gain.get("replaygain_track_gain"),
            replaygain_peak=gain.get("replaygain_track_peak"),
        )

    @classmethod
    def from_deezer(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata:
        """Build track metadata from a Deezer API track response."""
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
            artists=artists,
            bpm=round(resp["bpm"]) if resp.get("bpm") else None,
        )

    @classmethod
    def from_soundcloud(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata:
        """Build track metadata from a SoundCloud API track response."""
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
    def from_spotify(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata:
        """Build track metadata from a Spotify API track response."""
        artists = [a["name"] for a in resp.get("artists") or [] if a.get("name")]
        isrc = (resp.get("external_ids") or {}).get("isrc")
        return cls(
            TrackInfo(str(resp["id"]), bool(resp.get("explicit"))),
            (resp.get("name") or "Unknown").strip(),
            album,
            ", ".join(artists) or album.albumartist,
            resp.get("track_number", 1),
            resp.get("disc_number", 1),
            None,
            isrc=isrc.upper() if isrc else None,
            artists=artists or None,
        )

    @classmethod
    def from_tidal(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata:
        """Build track metadata from a Tidal API track response."""
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
            artists=artists,
            bpm=resp.get("bpm") or None,
            replaygain_gain=resp.get("replayGain"),
            replaygain_peak=resp.get("peak"),
            key=_key(resp.get("key"), resp.get("keyScale")),
        )

    @classmethod
    def from_resp(cls, album: AlbumMetadata, source, resp) -> TrackMetadata | None:
        """Dispatch to the from_* builder matching source."""
        if source not in SOURCES:
            raise ValueError(f"Invalid source {source}")
        return getattr(cls, f"from_{source}")(album, resp)

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
