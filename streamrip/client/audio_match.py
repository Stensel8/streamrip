"""Decide whether a search result is the track we want.

One module for every place streamrip compares tracks: a Spotify track with
YouTube Music's results, a row of a CSV list with a source's search results,
and a track of one service's playlist with another's (`streamrip playlist`).
It comes from Music-Sync (https://github.com/Stensel8/Music-Sync,
musicsync/matching.py), by the same author, which became part of streamrip.
The idea of looking a track up by its ISRC first and then scoring the
candidates on name, artist, album and length comes from spotDL
(https://github.com/spotDL/spotify-downloader).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

# ISO 3901: two letters (country), three letters or digits (registrant), seven
# digits (year and code).
_ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}\d{7}")


def normalize_isrc(value: str | None) -> str | None:
    """The canonical form of an ISRC ("gb-abc-12-34567" gives "GBABC1234567"),
    or None if it is not one."""
    cleaned = (value or "").replace("-", "").replace(" ", "").upper()
    return cleaned if _ISRC.fullmatch(cleaned) else None


@dataclass(slots=True)
class MatchTrack:
    """One recording as a service lists it: the track wanted, or a candidate."""

    title: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration_ms: int | None = None
    explicit: bool = False
    # Which service's id, and what kind of result a candidate is.
    video_id: str = ""
    is_song: bool = False
    # Identifies the recording itself, so it is the best way to find it again.
    isrc: str | None = None
    # Each service's own id for it, by service: {"spotify": "spotify:track:...",
    # "tidal": "123"}.
    ids: dict[str, str] = field(default_factory=dict)

    @property
    def artist(self) -> str:
        """The main (first) artist."""
        return self.artists[0] if self.artists else ""

    def __str__(self) -> str:
        return f"{', '.join(self.artists) or 'Unknown artist'} - {self.title}"


# Words that mark another version of a recording, with their spellings. A live version is not the studio
# version: when the two titles do not carry the same ones, the title score is halved.
VERSION_TAGS = frozendict(
    {
        "live": r"live|ao vivo|en vivo|en directo",  # also in Portuguese and Spanish
        "remix": r"remix(?:ed)?|rmx",
        "acoustic": r"acoustic|unplugged|stripped",
        "instrumental": r"instrumental",
        "a cappella": r"a ?cappella|acapella",
        "demo": r"demo",
        "karaoke": r"karaoke",
        "cover": r"cover",
        "radio edit": r"radio edit",
        "club mix": r"club (?:mix|edit)",
        "extended": r"extended",
        "sped up": r"sped[- ]?up|speed[- ]?up|nightcore",
        "slowed": r"slowed",
        "8d": r"8d(?: audio)?",
        "reprise": r"reprise",
        "re-recorded": r"re-?recorded|\w+'s version",  # "Love Story (Taylor's Version)" is not the 2008 recording
    }
)
_TAGS = frozendict(
    {
        tag: re.compile(rf"\b(?:{spellings})\b")
        for tag, spellings in VERSION_TAGS.items()
    }
)

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
# Curly quotes, grave and acute are apostrophes; an underscore is a space ("Live_Remastered").
_FOLDS = str.maketrans(dict.fromkeys("\u2019\u2018`\u00b4", "'") | {"_": " "})
# Words that services abbreviate differently: "Pt. 2" is "Part 2", "Rock 'n' Roll" is "Rock and Roll".
# Numbers become digits, so that "Part One", "Part I" and "Part 1" are one part and "Part Two" another:
# the words up to ten, and Roman numerals, except "i", "v" and "x", which are also words and letters.
_NUMBER_WORDS = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)
_SPELLINGS = frozendict(
    {"pt": "part", "vol": "volume", "n": "and"}
    | dict(
        zip(
            ["ii", "iii", "iv", "vi", "vii", "viii", "ix"],
            ["2", "3", "4", "6", "7", "8", "9"],
            strict=True,
        )
    )
    | {word: str(number) for number, word in enumerate(_NUMBER_WORDS, 1)}
)
# Those three letters are numerals too where a number is due: after one of these words ("Pt. I", "Act V") ...
_ROMAN_LETTERS = frozendict({"i": "1", "v": "5", "x": "10"})
_NUMBERED = frozenset(
    {
        "part",
        "volume",
        "chapter",
        "act",
        "book",
        "episode",
        "no",
        "number",
        "op",
        "opus",
    }
)
# ... and as the number of a movement: "I. Allegro", "V. Presto". Not in "U.N.I.T.Y." or "Mr. X".
_MOVEMENT = re.compile(r"(?<![\w.])([IVX])\.(?=\s)")
# What joins several artists in one name: "Macklemore & Ryan Lewis", "Nicky Jam x J Balvin",
# "Tom Petty and the Heartbreakers" (where the other service may have just "Tom Petty").
_ARTIST_JOINS = re.compile(
    r"\s*(?:[,;&+/]|\bx\b|\band\b|\bvs\b\.?|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\bwith\b)\s*"
)

# Weights of the three signals; they add up to 1.
TITLE_WEIGHT, ARTIST_WEIGHT, DURATION_WEIGHT = 0.5, 0.4, 0.1
# From this on two titles are the same song, whatever version each one is.
SAME_SONG = 0.6
# Below this the artist is simply someone else, whatever the title says. Names that share a few letters,
# like "Roy Blair" and "Radio Blazers" (0.64), stay under it.
MIN_ARTIST_SCORE = 0.7


# The text functions below are pure, and each wanted track is compared with many candidates: cache them.
# Bounded, as the web interface runs for a long time.
_cached = lru_cache(maxsize=4096)


@_cached
def _fold(text: str) -> str:
    """Lowercase and strip accents, so "Björk" equals "bjork". Curly apostrophes become straight ones."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_FOLDS))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _words(text: str) -> str:
    """Only the words of ``text``: "&" read as "and", apostrophes dropped ("don't" is "dont"), other
    punctuation gone, whitespace collapsed."""
    text = text.replace("&", " and ").replace("'", "")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


@_cached
def _comparable(text: str) -> str:
    """Folded words without remaster noise, with the usual abbreviations written out and numbers as digits."""
    text = _MOVEMENT.sub(lambda match: _ROMAN_LETTERS[match[1].lower()], text)
    words = [
        _SPELLINGS.get(word, word)
        for word in _words(_NOISE.sub(" ", _fold(text))).split()
    ]
    for at in range(1, len(words)):
        if words[at] in _ROMAN_LETTERS and words[at - 1] in _NUMBERED:
            words[at] = _ROMAN_LETTERS[words[at]]
    return " ".join(words)


def searchable(text: str) -> str:
    """`text` without accents and punctuation: another way to put it to a search
    engine."""
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
def version_tags(title: str) -> frozenset[str]:
    """The versions a title names: {"live"} for "Song (Live)"."""
    version = _fold(_version(title))
    return frozenset(tag for tag, pattern in _TAGS.items() if pattern.search(version))


@_cached
def _numbers(title: str) -> frozenset[tuple[int, int]]:
    """The numbers in a title, each with how often it came before: the 5 of "No. 5" is not the 5 of
    "V. Finale", so a title that has both is not covered by one that has the first alone."""
    numbers = [int(number) for number in re.findall(r"\d+", _full(title))]
    return frozenset(
        (number, numbers[:at].count(number)) for at, number in enumerate(numbers)
    )


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


def _apart(a: int | None, b: int | None) -> bool:
    """Whether two lengths are too far apart for one recording: two services list it
    seconds apart, a long one a little more."""
    return bool(a and b) and abs(a - b) > max(15_000, 0.1 * max(a, b))


def other_version(wanted: MatchTrack, candidate: MatchTrack) -> bool:
    """Whether ``candidate`` is another version than ``wanted``: live, a remix, an edit of another length."""
    return version_tags(wanted.title) != version_tags(candidate.title) or _apart(
        wanted.duration_ms, candidate.duration_ms
    )


def score(wanted: MatchTrack, candidate: MatchTrack) -> float:
    """How well ``candidate`` matches ``wanted``, from 0 to 1."""
    if _apart(wanted.duration_ms, candidate.duration_ms):
        return 0.0  # an edit or a mix of another length, whatever its title says
    title = _title_similarity(wanted.title, candidate.title)
    if version_tags(wanted.title) != version_tags(candidate.title):
        title *= 0.5  # another version: live, remix, ...
    duration = _duration_score(wanted.duration_ms, candidate.duration_ms)
    if not candidate.artists and wanted.artists:
        # Nobody to check the artist against (a video whose channel says nothing). The title and the
        # length alone would let another artist's "Hello" in under this one's tags.
        return 0.0
    if not wanted.artists:
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


def rank(wanted: MatchTrack, candidate: MatchTrack) -> tuple[float, float, float]:
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


def rejection(wanted: MatchTrack, candidate: MatchTrack) -> str:
    """Why `candidate`, which scored too low, is not `wanted`: "other song"
    (only the artist is the same), "other version" (live, remix and the like)
    or "low score" (close, but not close enough)."""
    if _title_similarity(wanted.title, candidate.title) < SAME_SONG:
        return "other song"
    if version_tags(wanted.title) != version_tags(candidate.title):
        return "other version"
    return "low score"


def best_match(
    wanted: MatchTrack, candidates: list[MatchTrack], min_score: float = 0.8
) -> tuple[MatchTrack, float] | None:
    """The best candidate and its score, if it is good enough."""
    best = max(candidates, key=lambda candidate: rank(wanted, candidate), default=None)
    if best is None or (found := score(wanted, best)) < min_score:
        return None
    return best, found
