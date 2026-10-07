"""Search results, the same shape whatever the source, for the menu."""

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .util import qobuz_artists

TIDAL_IMAGE = "https://resources.tidal.com/images/{uuid}/{size}x{size}.jpg"
RELEASE_TYPES = {
    "ALBUM": "Album",
    "EP": "EP",
    "SINGLE": "Single",
    "COMPILE": "Compilation",
}


@dataclass(slots=True)
class Summary:
    """One search result: its menu line, and what its preview shows."""

    media_type: str
    id: str
    name: str
    # Who made it; None for an artist.
    artist: str | None = None
    year: str | None = None
    explicit: bool = False
    # The preview's (label, value) lines, e.g. ("Tracks", "12").
    details: list[tuple[str, str]] = field(default_factory=list)
    description: str | None = None
    image_url: str | None = None
    image_fallback_urls: tuple[str, ...] = ()

    @property
    def image_urls(self) -> tuple[str, ...]:
        """Preferred preview URL followed by smaller alternatives."""
        return tuple(
            dict.fromkeys(
                url for url in (self.image_url, *self.image_fallback_urls) if url
            )
        )

    def summarize(self) -> str:
        """The result's line in the menu."""
        line = clean(self.name)
        if self.artist:
            line += f" by {clean(self.artist)}"
        notes = [n for n in (self.year, "explicit" if self.explicit else None) if n]
        return f"{line} ({', '.join(notes)})" if notes else line

    def __str__(self):
        """The summary as the search menu lists it."""
        return self.summarize()


def _details(*pairs) -> list[tuple[str, str]]:
    """The (label, value) pairs that have a value."""
    return [(label, str(value)) for label, value in pairs if value]


def _artist(item: dict) -> str:
    """The artist a search result of any source is credited to."""
    artist = (
        (item.get("performer") or {}).get("name")
        or item.get("artist")
        or (item.get("publisher_metadata") or {}).get("artist")
        or (item.get("user") or {}).get("username")
    )
    if isinstance(artist, dict):
        artist = artist.get("name")
    return artist or "Unknown"


def _track_artists(item: dict) -> str:
    """All of a track's artists, the way its tags will list them."""
    names = [a["name"] for a in item.get("artists") or [] if a.get("name")]
    return ", ".join(names or qobuz_artists(item)) or _artist(item)


def _album(item: dict) -> dict:
    """The item's album if it is a dict, else an empty one."""
    album = item.get("album")
    return album if isinstance(album, dict) else {}


def _day(value) -> str | None:
    """An ISO date or time, or a Unix time, as YYYY-MM-DD."""
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d")
    return str(value)[:10] if value else None


def _year(date) -> str | None:
    """The year of a date, or None."""
    day = _day(date)
    return day[:4] if day else None


def _duration(item: dict) -> str | None:
    """A length as m:ss or h:mm:ss, or None if there is none."""
    seconds = item.get("duration") or 0
    if item.get("kind") in ("track", "playlist"):  # SoundCloud counts in ms
        seconds //= 1000
    hours, rest = divmod(int(seconds), 3600)
    if not rest and not hours:
        return None
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def _genre(item: dict) -> str | None:
    """The genre of an item or of its album, whichever form the source uses."""
    genre = item.get("genre") or _album(item).get("genre")
    if isinstance(genre, dict):  # Qobuz
        return genre.get("name")
    if isinstance(genre, str):  # SoundCloud
        return genre or None
    names = [g.get("name") for g in item.get("genres") or [] if isinstance(g, dict)]
    return ", ".join(n for n in names if n) or None


def _label(item: dict) -> tuple[str, str | None]:
    """The record label, or (Tidal) the copyright line, which names it."""
    label = item.get("label") or _album(item).get("label")
    if isinstance(label, dict):
        label = label.get("name")
    return ("Label", label) if label else ("Copyright", item.get("copyright"))


def _key(item: dict) -> str | None:
    """A track's musical key (Tidal), such as "A minor"."""
    key, scale = item.get("key"), item.get("keyScale")
    if key:
        key = key.replace("Sharp", "♯").replace("Flat", "♭")
    return f"{key} {scale.lower()}" if key and scale else key


def _plain(text: str | None) -> str | None:
    """A description without Tidal's [wimpLink] or HTML markup."""
    text = re.sub(r"<br\s*/?>|</?p>", " ", text or "")  # line breaks
    text = re.sub(r"\[/?wimpLink[^\]]*\]|<[^>]+>", "", text)  # links
    return " ".join(html.unescape(text).split()) or None


def _quality(item: dict) -> str | None:
    """The best format the source says it has the item in."""
    bits = item.get("maximum_bit_depth")
    khz = item.get("maximum_sampling_rate")
    if bits and khz and bits > 0 and khz > 0:  # Qobuz
        return f"FLAC {bits}-bit / {khz:g} kHz"
    # Tidal only says whether it's hi-res; the sample rate is in each track's
    # stream info, a request of its own.
    tags = (item.get("mediaMetadata") or {}).get("tags") or []
    if "HIRES_LOSSLESS" in tags:
        quality = "FLAC 24-bit, up to 192 kHz"
    elif "LOSSLESS" in tags:
        quality = "FLAC 16-bit / 44.1 kHz"
    else:
        return None
    return quality + (", Dolby Atmos" if "DOLBY_ATMOS" in tags else "")


def _explicit(item: dict) -> bool:
    """Whether any of the sources' explicit flags is set."""
    return bool(
        item.get("parental_warning")
        or item.get("explicit")
        or item.get("explicit_lyrics")
        or (item.get("publisher_metadata") or {}).get("explicit")
    )


def _image_urls(item: dict) -> tuple[str, ...]:
    """Preview-sized covers, best first, with the source's smaller fallbacks."""
    album = _album(item)
    qobuz = item.get("image") if isinstance(item.get("image"), dict) else None
    qobuz = qobuz or album.get("image")
    if isinstance(qobuz, dict):
        return tuple(
            dict.fromkeys(
                qobuz[k]
                for k in ("large", "medium", "small", "thumbnail")
                if qobuz.get(k)
            )
        )
    playlist = [
        item[k][0]
        for k in ("images600", "images300", "images150", "images")
        if item.get(k)
    ]
    if playlist:
        return tuple(dict.fromkeys(playlist))
    deezer = [
        item.get(key) or album.get(key)
        for key in (
            "cover_big",
            "picture_big",
            "cover_medium",
            "picture_medium",
            "cover_small",
            "picture_small",
        )
    ]
    if any(deezer):
        return tuple(dict.fromkeys(url for url in deezer if url))
    uuid = (
        item.get("cover")
        or item.get("squareImage")
        or item.get("picture")
        or album.get("cover")
    )
    if isinstance(uuid, str) and "-" in uuid and "/" not in uuid:
        return tuple(
            TIDAL_IMAGE.format(uuid=uuid.replace("-", "/"), size=size)
            for size in (640, 320, 160)
        )
    url = item.get("artwork_url") or (item.get("user") or {}).get("avatar_url")
    if url:
        return tuple(dict.fromkeys((url.replace("-large.", "-t500x500."), url)))
    return ()


def _image(item: dict) -> str | None:
    """The preferred cover URL; retained for callers inspecting summaries."""
    urls = _image_urls(item)
    return urls[0] if urls else None


def album_summary(item: dict) -> Summary:
    """Summarize an album from any source's search response."""
    title = (item.get("title") or "").strip()
    version = (item.get("version") or "").strip()
    date = (
        item.get("release_date_original")
        or item.get("releaseDate")
        or item.get("release_date")
        or item.get("display_date")
    )
    tracks = (
        item.get("tracks_count") or item.get("numberOfTracks") or item.get("nb_tracks")
    )
    discs = item.get("media_count") or item.get("numberOfVolumes") or 1
    if tracks and discs > 1:
        tracks = f"{tracks} ({discs} discs)"
    kind = str(item.get("record_type") or item.get("type") or "").upper()
    return Summary(
        "album",
        str(item["id"]),
        f"{title} ({version})" if version else title,
        _artist(item),
        year=_year(date),
        explicit=_explicit(item),
        details=_details(
            ("Released", _day(date)),
            ("Type", RELEASE_TYPES.get(kind)),
            ("Tracks", tracks),
            ("Length", _duration(item)),
            ("Genre", _genre(item)),
            _label(item),
            ("Quality", _quality(item)),
        ),
        description=_plain(item.get("description")),
        image_url=_image(item),
        image_fallback_urls=_image_urls(item)[1:],
    )


def track_summary(item: dict) -> Summary:
    """Summarize a track from any source's search response."""
    name = (item.get("title") or item.get("name") or "Unknown").strip()
    if (version := item.get("version")) and version not in name:
        name = f"{name} ({version})"
    album = _album(item)
    date = (
        item.get("release_date_original")
        or album.get("release_date_original")
        or item.get("streamStartDate")
        or item.get("release_date")
        or item.get("display_date")
    )
    return Summary(
        "track",
        str(item["id"]),
        name,
        _track_artists(item),
        year=_year(date),
        explicit=_explicit(item),
        details=_details(
            ("Album", album.get("title")),
            ("Track", item.get("track_number") or item.get("trackNumber")),
            ("Released", _day(date)),
            ("Length", _duration(item)),
            ("Genre", _genre(item)),
            _label(item),
            ("Composer", (item.get("composer") or {}).get("name")),
            ("BPM", item.get("bpm")),
            ("Key", _key(item)),
            ("Quality", _quality(item)),
        ),
        description=_plain(item.get("description")),
        image_url=_image(item),
        image_fallback_urls=_image_urls(item)[1:],
    )


def artist_summary(item: dict) -> Summary:
    """Summarize an artist from any source's search response."""
    roles = [r.get("category") for r in item.get("artistRoles") or []]  # Tidal
    popularity = item.get("popularity")  # Tidal, 0-100
    return Summary(
        "artist",
        str(item["id"]),
        item.get("name") or _artist(item),
        details=_details(
            ("Albums", item.get("albums_count") or item.get("nb_album")),
            ("Fans", item.get("nb_fan") or item.get("followers_count")),
            ("Roles", ", ".join(r for r in roles if r)),
            ("Popularity", popularity and f"{popularity}/100"),
        ),
        image_url=_image(item),
        image_fallback_urls=_image_urls(item)[1:],
    )


def playlist_summary(item: dict) -> Summary:
    """Summarize a playlist from any source's search response."""
    user = item.get("user") or {}
    creator = (
        (item.get("owner") or {}).get("name")  # Qobuz
        or user.get("name")  # Deezer
        or user.get("username")  # SoundCloud
        or (item.get("publisher_metadata") or {}).get("artist")
        or ("TIDAL" if item.get("type") == "EDITORIAL" else None)
        or "Unknown"
    )
    tracks = (
        item.get("tracks_count")
        or item.get("numberOfTracks")
        or item.get("nb_tracks")
        or item.get("track_count")
        or len(item.get("tracks") or [])
    )
    updated = (
        item.get("lastUpdated")
        or item.get("updated_at")
        or item.get("mod_date")
        or item.get("last_modified")
    )
    return Summary(
        "playlist",
        str(item.get("id") or item.get("uuid") or "Unknown"),
        item.get("name") or item.get("title") or "Unknown",
        creator,
        details=_details(
            ("Tracks", tracks),
            ("Length", _duration(item)),
            ("Genre", _genre(item)),
            ("Followers", item.get("users_count") or item.get("likes_count")),
            ("Updated", _day(updated)),
        ),
        description=_plain(item.get("description")),
        image_url=_image(item),
        image_fallback_urls=_image_urls(item)[1:],
    )


SUMMARIES = {
    "track": track_summary,
    "album": album_summary,
    "artist": artist_summary,
    "playlist": playlist_summary,
}


def _page_items(source: str, media_type: str, page: dict) -> list[dict]:
    """The results in one page of a source's search response."""
    if source == "soundcloud":
        return page["collection"]
    if source == "qobuz":
        return page[f"{media_type}s"]["items"]
    if source == "deezer":
        return page["data"]
    if source in ("tidal", "spotify"):
        return page["items"]
    raise NotImplementedError(source)


@dataclass(slots=True)
class SearchResults:
    results: list[Summary]

    @classmethod
    def from_pages(cls, source: str, media_type: str, pages: list[dict]):
        """A summary of every item on a source's search pages, for `media_type`."""
        summarize = SUMMARIES.get(media_type)
        if summarize is None:
            raise Exception(f"invalid media type {media_type}")
        return cls(
            [
                summarize(item)
                for page in pages
                for item in _page_items(source, media_type, page)
            ]
        )

    def summaries(self) -> list[str]:
        """The numbered one-line summaries the search menu shows."""
        return [f"{i + 1}. {r.summarize()}" for i, r in enumerate(self.results)]

    def get_choices(self, inds: tuple[int, ...] | int):
        if isinstance(inds, int):
            inds = (inds,)
        return [self.results[i] for i in inds]

    def as_list(self, source: str) -> list[dict[str, str]]:
        """The results as dicts: source, media type, id and description."""
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
