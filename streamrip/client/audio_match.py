"""Decide whether a YouTube Music result is the Spotify track we want.

Adapted from the matching of Music-Sync (https://github.com/Stensel8/Music-Sync,
musicsync/matching.py), written by the same author. The idea of looking a track
up by its ISRC first and then scoring the candidates on name, artist, album and
length comes from spotDL (https://github.com/spotDL/spotify-downloader).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache


@dataclass(slots=True)
class MatchTrack:
    """One recording as a service lists it: the wanted Spotify track, or a candidate."""

    title: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration_ms: int | None = None
    explicit: bool = False
    # Which service's id, and what kind of result a candidate is.
    video_id: str = ""
    is_song: bool = False

    @property
    def artist(self) -> str:
        """The main (first) artist."""
        return self.artists[0] if self.artists else ""

    def __str__(self) -> str:
        return f"{', '.join(self.artists) or 'Unknown artist'} - {self.title}"


Track = MatchTrack

# Words that mark another version of a recording, with their spellings. A live version is not the studio
# version: when the two titles do not carry the same ones, the title score is halved.
VERSION_TAGS = {
    "live": r"live",
    "remix": r"remix|rmx",
    "acoustic": r"acoustic|unplugged",
    "instrumental": r"instrumental",
    "a cappella": r"a ?cappella|acapella",
    "demo": r"demo",
    "karaoke": r"karaoke",
    "cover": r"cover",
    "radio edit": r"radio edit",
    "extended": r"extended",
    "sped up": r"sped up",
    "slowed": r"slowed",
    "re-recorded": r"re-?recorded|\w+'s version",  # "Love Story (Taylor's Version)" is not the 2008 recording
}
_TAGS = {
    tag: re.compile(rf"\b(?:{spellings})\b") for tag, spellings in VERSION_TAGS.items()
}

_BRACKETS = re.compile(r"[(\[{][^)\]}]*[)\]}]")
_FEATURING = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_BRACKETED_FEATURING = re.compile(
    r"[(\[{]\s*(?:feat\.?|ft\.?|featuring|with)\s[^)\]}]*[)\]}]", re.IGNORECASE
)
_DASH_SUFFIX = re.compile(
    r"\s+[-\u2013\u2014]\s+.*$"
)  # "Song - Remastered 2011", also with an en or em dash
_NOISE = re.compile(
    r"\b(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+version)?(?:\s+\d{4})?\b"
    r"|\b(?:mono|stereo)(?:\s+version)?\b|\bbonus track\b|\bdeluxe(?: edition| version)?\b|\bexplicit\b"
    r"|\b(?:single|album|original|lp) version\b|\boriginal mix\b",
    re.IGNORECASE,
)
_APOSTROPHES = str.maketrans(
    dict.fromkeys("\u2019\u2018`\u00b4", "'")
)  # curly quotes, grave, acute
# Words that services abbreviate differently: "Pt. 2" is "Part 2", "Rock 'n' Roll" is "Rock and Roll".
# Roman numerals are numbers too, except "i", "v" and "x", which are also words and letters.
_SPELLINGS = {"pt": "part", "vol": "volume", "n": "and"} | dict(
    zip(
        ["ii", "iii", "iv", "vi", "vii", "viii", "ix"],
        ["2", "3", "4", "6", "7", "8", "9"],
        strict=True,
    )
)
# What joins several artists in one name: "Macklemore & Ryan Lewis", "Nicky Jam x J Balvin",
# "Tom Petty and the Heartbreakers" (where the other service may have just "Tom Petty").
_ARTIST_JOINS = re.compile(
    r"\s*(?:[,;&+]|\bx\b|\band\b|\bvs\b\.?|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\bwith\b)\s*"
)

# Weights of the three signals; they add up to 1.
TITLE_WEIGHT, ARTIST_WEIGHT, DURATION_WEIGHT = 0.5, 0.4, 0.1
# Below this the artist is simply someone else, whatever the title says. Names that share a few letters,
# like "Roy Blair" and "Radio Blazers" (0.64), stay under it.
MIN_ARTIST_SCORE = 0.7


# The text functions below are pure, and each wanted track is compared with many candidates: cache them.
# Bounded, as the web interface runs for a long time.
_cached = lru_cache(maxsize=4096)


@_cached
def _fold(text: str) -> str:
    """Lowercase and strip accents, so "Björk" equals "bjork". Curly apostrophes become straight ones."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_APOSTROPHES))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _words(text: str) -> str:
    """Only the words of ``text``: "&" read as "and", apostrophes dropped ("don't" is "dont"), other
    punctuation gone, whitespace collapsed."""
    text = text.replace("&", " and ").replace("'", "")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


@_cached
def _comparable(text: str) -> str:
    """Folded words without remaster noise, with the usual abbreviations written out."""
    words = _words(_NOISE.sub(" ", _fold(text))).split()
    return " ".join(_SPELLINGS.get(word, word) for word in words)


def searchable(text: str) -> str:
    """``text`` without accents and punctuation: another way to put it to a search engine."""
    return _words(_fold(text))


def simplify_title(title: str) -> str:
    """Drop bracketed parts, "feat." clauses and " - Remastered" style suffixes, keeping the case.
    A title that is nothing but such parts, like "(Untitled)", is kept as it is."""
    simple = _BRACKETS.sub(" ", title)
    simple = _DASH_SUFFIX.sub("", simple)
    return " ".join(_FEATURING.sub("", simple).split()) or " ".join(title.split())


@_cached
def normalize(title: str) -> str:
    """The comparable form of the main part of a title."""
    return _comparable(simplify_title(title))


@_cached
def _full(title: str) -> str:
    """The comparable form of the whole title: bracketed parts kept, only "feat." parts left out.
    It catches titles whose main part is in brackets, like "(I Can't Get No) Satisfaction"."""
    return _comparable(_FEATURING.sub("", _BRACKETED_FEATURING.sub(" ", title)))


@_cached
def similarity(a: str, b: str) -> float:
    """How alike two comparable strings are, from 0 to 1. Spaces and word order count for little."""
    if a.replace(" ", "") == b.replace(" ", ""):
        return 1.0
    in_order = SequenceMatcher(None, a, b).ratio()
    any_order = SequenceMatcher(
        None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    ).ratio()
    return max(in_order, any_order)


def _version(title: str) -> str:
    """The parts of a title that can name a version: what is in brackets and what follows " - ". Not the
    main title, or the studio "Live Wire" would carry the same "live" as "Live Wire (Live)"."""
    parts = _BRACKETS.findall(title)
    if dash := _DASH_SUFFIX.search(_BRACKETS.sub(" ", title)):
        parts.append(dash.group())
    return " ".join(parts)


@_cached
def _tags(title: str) -> frozenset[str]:
    version = _fold(_version(title))
    return frozenset(tag for tag, pattern in _TAGS.items() if pattern.search(version))


@_cached
def _numbers(title: str) -> frozenset[int]:
    return frozenset(int(number) for number in re.findall(r"\d+", _full(title)))


def _title_similarity(wanted: str, candidate: str) -> float:
    """How alike two titles are, whatever version each one is. Titles with other numbers are other songs:
    "Part 1" and "Part 2", "Song 5" and "Song 55". A number on one side only, like a year, is no matter."""
    alike = max(
        similarity(normalize(wanted), normalize(candidate)),
        similarity(_full(wanted), _full(candidate)),
    )
    ours, theirs = _numbers(wanted), _numbers(candidate)
    return alike if ours <= theirs or theirs <= ours else alike * 0.5


@_cached
def _artist_key(name: str) -> str:
    key = _words(name.replace("$", "s"))  # Ke$ha, A$AP Rocky
    return key.removeprefix("the ")


def _artist_keys(names: list[str]) -> set[str]:
    """The comparable form of each name, plus the parts of names that join several artists: one service
    has "Macklemore & Ryan Lewis" where the other lists "Macklemore" and "Ryan Lewis"."""
    keys = set()
    for name in map(_fold, names):
        keys.add(_artist_key(name))
        # Parts of one or two letters ("Lil Nas X" gives "x") would match too much.
        keys.update(
            key
            for part in _ARTIST_JOINS.split(name)
            if len(key := _artist_key(part)) > 2
        )
    keys.discard("")
    return keys


def _artist_score(wanted: list[str], candidate: list[str]) -> float:
    """1.0 when an artist on one side is an artist on the other, else the best similarity."""
    ours, theirs = _artist_keys(wanted), _artist_keys(candidate)
    if ours & theirs:
        return 1.0
    return max((similarity(a, b) for a in ours for b in theirs), default=0.0)


def _duration_score(a: int | None, b: int | None) -> float:
    if a is None or b is None:
        return 0.8  # unknown: slightly below a confirmed match
    difference = abs(a - b)
    return 1.0 if difference <= 3000 else 0.5 if difference <= 10_000 else 0.0


def score(wanted: Track, candidate: Track) -> float:
    """How well ``candidate`` matches ``wanted``, from 0 to 1."""
    title = _title_similarity(wanted.title, candidate.title)
    if _tags(wanted.title) != _tags(
        candidate.title
    ):  # another version: live, remix, ...
        title *= 0.5
    duration = _duration_score(wanted.duration_ms, candidate.duration_ms)
    if not wanted.artists or not candidate.artists:
        # Nothing to compare the artist with (a CSV of titles only): judge by the title and the length.
        return round(
            (TITLE_WEIGHT * title + DURATION_WEIGHT * duration)
            / (TITLE_WEIGHT + DURATION_WEIGHT),
            4,
        )
    artist = _artist_score(wanted.artists, candidate.artists)
    if artist < MIN_ARTIST_SCORE:
        return 0.0
    return round(
        TITLE_WEIGHT * title + ARTIST_WEIGHT * artist + DURATION_WEIGHT * duration, 4
    )


def rank(wanted: Track, candidate: Track) -> tuple[float, float, float]:
    """Orders candidates: by score, then, among equals, by the closest full title ("Song (A Remix)" over
    "Song (B Remix)"), then by the same album (the original release over a compilation)."""
    album = (
        similarity(normalize(wanted.album), normalize(candidate.album))
        if wanted.album and candidate.album
        else 0
    )
    return (
        score(wanted, candidate),
        similarity(_full(wanted.title), _full(candidate.title)),
        album,
    )
