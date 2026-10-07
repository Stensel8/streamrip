"""Lyrics for a track whose source sent none, from LRCLIB (https://lrclib.net).

LRCLIB is a free, open database of plain and time-synced (LRC) lyrics that its
users add. It needs no account or key. A lookup does tell it the artist, title
and length of the track, so `[downloads] lyrics = false` keeps it from happening.

A track is asked for by its artist, title and length (`/api/get`, which takes a
length within two seconds, so another edit of the song is not taken for it). When
LRCLIB has no record by exactly those, it is searched for, and the results are
scored the way a YouTube Music result is (see `client.audio_match`), with a limit
on how far the length may be off: synced lyrics only fit one edit of a song.
"""

import asyncio
import logging
import re

import aiohttp

from .client import new_session
from .client.audio_match import MatchTrack, rank, score, simplify_title

logger = logging.getLogger("streamrip")

API = "https://lrclib.net/api"
TIMEOUT = aiohttp.ClientTimeout(total=15, sock_connect=10)
# Requests at once; a community service asks for no more than it needs.
CONCURRENCY = 3
# After this many lookups in a row that fail, LRCLIB is left alone for the run.
MAX_FAILURES = 3
# From this score on a search result is the track (as with a YouTube Music result).
MIN_SCORE = 0.8
# How far, in seconds, a record's length may be from the audio's. LRCLIB's own
# exact lookup allows two.
MAX_LENGTH_DIFFERENCE = 3.0
LEFT_ALONE = "LRCLIB is left alone for the rest of the run"


class LyricsUnavailableError(Exception):
    """LRCLIB did not answer, or is left alone for the rest of the run.

    That says nothing about the lyrics of a track: LRCLIB may well have them.
    """


def _user_agent() -> str:
    """What LRCLIB asks for: a User-Agent that says what is asking."""
    # Not imported at the top: the package imports this module while it is still
    # being set up, before it has a version.
    from . import __version__

    return f"streamrip/{__version__} (https://github.com/Stensel8/streamrip)"


def _without_timestamps(synced: str) -> str:
    """Synced (LRC) lyrics as plain lines: "[00:12.34] words" becomes "words"."""
    return "\n".join(
        re.sub(r"^(?:\[[^\]]*\]\s*)+", "", line) for line in synced.splitlines()
    ).strip()


def lyrics_of(record: dict, plain: bool) -> str | None:
    """The lyrics in a LRCLIB record: the synced (LRC) ones, or plain ones if `plain`.

    Whichever there is when the wanted kind is not: LRC lines can be made plain.
    """
    if record.get("instrumental"):
        return None
    synced = (record.get("syncedLyrics") or "").strip()
    unsynced = (record.get("plainLyrics") or "").strip()
    if plain:
        return unsynced or _without_timestamps(synced) or None
    return synced or unsynced or None


def _best(
    wanted: MatchTrack, seconds: float | None, records: list[dict]
) -> dict | None:
    """The search result that is `wanted`, if there is one."""
    candidates = []
    for record in records:
        if not isinstance(record, dict) or lyrics_of(record, plain=False) is None:
            continue
        length = record.get("duration")
        if seconds and abs((length or 0) - seconds) > MAX_LENGTH_DIFFERENCE:
            continue  # another edit: the words, and the timing, may differ
        candidate = MatchTrack(
            record.get("trackName") or "",
            [record.get("artistName") or ""],
            record.get("albumName") or "",
            int(length * 1000) if length else None,
        )
        candidates.append((candidate, record))
    best = max(candidates, key=lambda c: rank(wanted, c[0]), default=None)
    if best is None or score(wanted, best[0]) < MIN_SCORE:
        return None
    return best[1]


class Lrclib:
    """A session to LRCLIB, what it has said, and whether it is worth asking."""

    def __init__(self, verify_ssl: bool = True):
        """Open a session of its own: never a source's, which holds its login."""
        self._session = new_session(verify_ssl=verify_ssl, timeout=TIMEOUT)
        self._headers = {"User-Agent": _user_agent()}
        self._slots = asyncio.Semaphore(CONCURRENCY)
        self._records: dict[tuple, dict | None] = {}
        self._failures = 0

    async def _get(self, path: str, params: dict):
        """The JSON of a request, or None for "there is none"."""
        async with self._slots:
            # Looked at again here: a lookup that waited for a slot was let in
            # before the failures that closed the door.
            if self._failures >= MAX_FAILURES:
                raise LyricsUnavailableError(LEFT_ALONE)
            async with self._session.get(
                f"{API}/{path}", params=params, headers=self._headers
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.json(content_type=None)

    async def record(self, wanted: MatchTrack, seconds: float | None) -> dict | None:
        """The LRCLIB record of a track, or None if it has none.

        Raises LyricsUnavailableError when LRCLIB does not answer, or is left alone.
        """
        key = (wanted.artist.casefold(), wanted.title.casefold(), round(seconds or 0))
        if key in self._records:
            return self._records[key]
        if self._failures >= MAX_FAILURES:
            raise LyricsUnavailableError(LEFT_ALONE)
        try:
            params = {"artist_name": wanted.artist, "track_name": wanted.title}
            if seconds:
                params["duration"] = round(seconds)
            record = await self._get("get", params)
            if record is None:
                found = await self._get(
                    "search",
                    {
                        "track_name": simplify_title(wanted.title),
                        "artist_name": wanted.artist,
                    },
                )
                record = _best(
                    wanted, seconds, found if isinstance(found, list) else []
                )
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            self._failures += 1
            logger.debug(f"LRCLIB lookup for {wanted} failed: {type(e).__name__}: {e}")
            if self._failures == MAX_FAILURES:
                logger.warning(
                    "LRCLIB does not answer; no more lyrics are looked up this run"
                )
            raise LyricsUnavailableError(
                f"LRCLIB did not answer ({type(e).__name__})"
            ) from e
        self._failures = 0
        self._records[key] = record if isinstance(record, dict) else None
        return self._records[key]

    async def close(self):
        await self._session.close()


# One for the whole run, made when it is first needed: the session belongs to the
# event loop, and `close` (see Main.__aexit__) ends it.
_lrclib: Lrclib | None = None


async def find_lyrics(
    wanted: MatchTrack, seconds: float | None, plain: bool, verify_ssl: bool = True
) -> str | None:
    """The lyrics of a track on LRCLIB, or None if it has none there.

    `seconds` is the audio's length. Raises LyricsUnavailableError when LRCLIB did not
    answer: that is no "none".
    """
    global _lrclib
    if _lrclib is None:
        _lrclib = Lrclib(verify_ssl)
    record = await _lrclib.record(wanted, seconds)
    if record is None:
        logger.debug(f"No lyrics for {wanted} on LRCLIB")
        return None
    lyrics = lyrics_of(record, plain)
    if lyrics is not None:
        logger.debug(f"Found lyrics for {wanted} on LRCLIB")
    return lyrics


async def close():
    """Close the session to LRCLIB, if there is one. Fine to call twice."""
    global _lrclib
    lrclib, _lrclib = _lrclib, None
    if lrclib is not None:
        await lrclib.close()
