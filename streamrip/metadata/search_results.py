import os
import re
import textwrap
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


def _artist(item: dict) -> str:
    """The artist a search result of any source is credited to."""
    artist = (
        (item.get("performer") or {}).get("name")
        or item.get("artist")
        or (item.get("publisher_metadata") or {}).get("artist")
    )
    if isinstance(artist, dict):
        artist = artist.get("name")
    return artist or "Unknown"


class Summary(ABC):
    id: str
    media_type: ClassVar[str]

    @abstractmethod
    def summarize(self) -> str:
        pass

    @abstractmethod
    def preview(self) -> str:
        pass

    @classmethod
    @abstractmethod
    def from_item(cls, item: dict) -> "Summary":
        pass

    def __str__(self):
        return self.summarize()


@dataclass(slots=True)
class ArtistSummary(Summary):
    id: str
    name: str
    num_albums: str | None
    media_type: ClassVar[str] = "artist"

    def summarize(self) -> str:
        return clean(self.name)

    def preview(self) -> str:
        # Not every source's artist search includes an album count (Tidal's
        # doesn't at all), so don't print a made-up "Unknown Albums" -- just
        # leave the line out rather than claim something we don't know.
        if self.num_albums is None:
            return f"ID: {self.id}"
        return f"{self.num_albums} Albums\n\nID: {self.id}"

    @classmethod
    def from_item(cls, item: dict):
        name = item.get("name") or _artist(item)
        return cls(str(item["id"]), name, item.get("albums_count"))


@dataclass(slots=True)
class TrackSummary(Summary):
    id: str
    name: str
    artist: str
    date_released: str | None
    media_type: ClassVar[str] = "track"

    def summarize(self) -> str:
        return f"{clean(self.name)} by {clean(self.artist)}"

    def preview(self) -> str:
        return f"Released on:\n{self.date_released}\n\nID: {self.id}"

    @classmethod
    def from_item(cls, item: dict):
        name = item.get("title") or item.get("name") or "Unknown"
        date_released = (
            item.get("release_date")
            or item.get("streamStartDate")
            or (item.get("album") or {}).get("release_date_original")
            or item.get("display_date")
            or item.get("date")
            or item.get("year")
            or "Unknown"
        )
        return cls(str(item["id"]), name.strip(), _artist(item), date_released)


@dataclass(slots=True)
class AlbumSummary(Summary):
    id: str
    name: str
    artist: str
    num_tracks: str
    date_released: str | None
    media_type: ClassVar[str] = "album"

    def summarize(self) -> str:
        return f"{clean(self.name)} by {clean(self.artist)}"

    def preview(self) -> str:
        return f"Date released:\n{self.date_released}\n\n{self.num_tracks} Tracks\n\nID: {self.id}"

    @classmethod
    def from_item(cls, item: dict):
        title = (item.get("title") or "").strip()
        version = (item.get("version") or "").strip()
        name = f"{title} ({version})" if version else title
        num_tracks = (
            item.get("tracks_count")
            or item.get("numberOfTracks")
            or len(item.get("tracks") or item.get("items") or [])
        )
        date_released = (
            item.get("release_date_original")
            or item.get("release_date")
            or item.get("releaseDate")
            or item.get("display_date")
            or item.get("date")
            or item.get("year")
            or "Unknown"
        )
        return cls(str(item["id"]), name, _artist(item), str(num_tracks), date_released)


@dataclass(slots=True)
class PlaylistSummary(Summary):
    id: str
    name: str
    creator: str
    num_tracks: int
    description: str
    media_type: ClassVar[str] = "playlist"

    def summarize(self) -> str:
        return f"{clean(self.name)} by {clean(self.creator)}"

    def preview(self) -> str:
        desc = clean(self.description, trunc=False)
        wrapped = "\n".join(
            textwrap.wrap(desc, os.get_terminal_size().columns - 4 or 70),
        )
        return f"{self.num_tracks} tracks\n\nDescription:\n{wrapped}\n\nID: {self.id}"

    @classmethod
    def from_item(cls, item: dict):
        id = item.get("id") or item.get("uuid") or "Unknown"
        name = item.get("name") or item.get("title") or "Unknown"
        user = item.get("user") or {}
        creator = (
            (item.get("publisher_metadata") or {}).get("artist")
            or (item.get("owner") or {}).get("name")
            or user.get("username")
            or user.get("name")
            or "Unknown"
        )
        num_tracks = (
            item.get("tracks_count")
            or item.get("nb_tracks")
            or item.get("numberOfTracks")
            or len(item.get("tracks", []))
            or -1
        )
        description = item.get("description") or "No description"
        return cls(id, name, creator, num_tracks, description)


SUMMARY_TYPES: dict[str, type[Summary]] = {
    "track": TrackSummary,
    "album": AlbumSummary,
    "artist": ArtistSummary,
    "playlist": PlaylistSummary,
}


def _page_items(source: str, media_type: str, page: dict) -> list[dict]:
    """The results in one page of a source's search response."""
    if source == "soundcloud":
        return page["collection"]
    if source == "qobuz":
        return page[f"{media_type}s"]["items"]
    if source == "deezer":
        return page["data"]
    if source == "tidal":
        return page["items"]
    raise NotImplementedError(source)


@dataclass(slots=True)
class SearchResults:
    results: list[Summary]

    @classmethod
    def from_pages(cls, source: str, media_type: str, pages: list[dict]):
        summary_type = SUMMARY_TYPES.get(media_type)
        if summary_type is None:
            raise Exception(f"invalid media type {media_type}")
        return cls(
            [
                summary_type.from_item(item)
                for page in pages
                for item in _page_items(source, media_type, page)
            ]
        )

    def summaries(self) -> list[str]:
        return [f"{i + 1}. {r.summarize()}" for i, r in enumerate(self.results)]

    def get_choices(self, inds: tuple[int, ...] | int):
        if isinstance(inds, int):
            inds = (inds,)
        return [self.results[i] for i in inds]

    def preview(self, s: str) -> str:
        ind = re.match(r"^\d+", s)
        assert ind is not None
        i = int(ind.group(0))
        return self.results[i - 1].preview()

    def as_list(self, source: str) -> list[dict[str, str]]:
        return [
            {
                "source": source,
                "media_type": i.media_type,
                "id": i.id,
                "desc": i.summarize(),
            }
            for i in self.results
        ]


def clean(s: str, trunc=True) -> str:
    """s without "|" or newlines (they break the menu), cut to 50 characters."""
    s = s.replace("|", "").replace("\n", "")
    return s[:50] if trunc else s
