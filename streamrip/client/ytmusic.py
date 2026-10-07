"""Find the YouTube Music result that is a given Spotify track.

Spotify only supplies the metadata; the audio comes from YouTube Music. Like
spotDL (https://github.com/spotDL/spotify-downloader), a track is looked up by
its ISRC first, and then by its artist and title; the candidates are scored by
`audio_match`, which also rejects other versions (live, remix, ...).
"""

import asyncio
import logging
import re
from dataclasses import dataclass

from .audio_match import MatchTrack, rank, score

logger = logging.getLogger("streamrip")

WATCH_URL = "https://music.youtube.com/watch?v={video_id}"

# From this score on a result is the track (see audio_match.best_match).
MIN_SCORE = 0.8
# An ISRC hit this good is taken without a second search.
SURE_SCORE = 0.9
SEARCH_LIMIT = 20
SEARCH_ATTEMPTS = 2
# YouTube Music searches at once; more only gets them refused.
SEARCH_CONCURRENCY = 3
# The same recording is listed with the same length, give or take a moment of
# silence. A song gets more room: its edit is also told apart by its title. A
# video can start with an intro or end with credits, so it gets less.
MAX_SONG_LENGTH_DIFFERENCE = 20_000  # ms
MAX_VIDEO_LENGTH_DIFFERENCE = 10_000

# "Artist - Title", the way a video is titled.
_VIDEO_TITLE = re.compile(r"^(?P<artist>.+?)\s+[-\u2013\u2014]\s+(?P<title>.+)$")
# A bracket of a video title that only says what kind of video it is. One that
# says anything else too, like "(Official Remix)", stays: it names a version.
_VIDEO_NOISE = re.compile(
    r"\s*[(\[]\s*(?:official|music|lyrics?|video|audio|visuali[sz]er|hd|4k|hq|mv"
    r"|version|full|\s|\|)+[)\]]",
    re.IGNORECASE,
)
_CHANNEL_NOISE = re.compile(r"\s*-\s*topic$|vevo$", re.IGNORECASE)


@dataclass(slots=True)
class AudioMatch:
    """The YouTube Music result chosen for a track."""

    track: MatchTrack
    score: float

    @property
    def url(self) -> str:
        """The URL yt-dlp downloads the audio from."""
        return WATCH_URL.format(video_id=self.track.video_id)


def _artist_names(result: dict) -> list[str]:
    return [a["name"] for a in result.get("artists") or [] if a.get("name")]


def _video_candidate(result: dict, video_id: str, ms: int | None) -> MatchTrack:
    """A video as a track: the artist is in its title more often than in its channel."""
    title = _VIDEO_NOISE.sub("", result["title"]).strip()
    channels = [_CHANNEL_NOISE.sub("", name) for name in _artist_names(result)]
    artists = [name for name in channels if name]
    if split := _VIDEO_TITLE.match(title):
        artists = [split["artist"], *artists]
        title = split["title"]
    return MatchTrack(title, artists, duration_ms=ms, video_id=video_id, is_song=False)


def candidates_from(results: list[dict]) -> list[MatchTrack]:
    """The songs and videos of a YouTube Music search as candidates, in order."""
    found: dict[str, MatchTrack] = {}
    for result in results:
        video_id, title = result.get("videoId"), result.get("title")
        if not video_id or not title or video_id in found:
            continue
        seconds = result.get("duration_seconds")
        ms = int(seconds * 1000) if seconds else None
        kind = result.get("resultType")
        if kind == "song":
            found[video_id] = MatchTrack(
                title,
                _artist_names(result),
                album=(result.get("album") or {}).get("name") or "",
                duration_ms=ms,
                explicit=bool(result.get("isExplicit")),
                video_id=video_id,
                is_song=True,
            )
        elif kind == "video":
            found[video_id] = _video_candidate(result, video_id, ms)
    return list(found.values())


def _length_fits(wanted: MatchTrack, candidate: MatchTrack) -> bool:
    if wanted.duration_ms is None or candidate.duration_ms is None:
        return True
    limit = (
        MAX_SONG_LENGTH_DIFFERENCE if candidate.is_song else MAX_VIDEO_LENGTH_DIFFERENCE
    )
    return abs(wanted.duration_ms - candidate.duration_ms) <= limit


def select(
    wanted: MatchTrack, candidates: list[MatchTrack], min_score: float = MIN_SCORE
) -> AudioMatch | None:
    """The candidate that is `wanted`, if one is good enough.

    Of equally good ones the first wins, so list the best source first. A song
    beats a video, and the edition with the same explicit flag beats the other.
    """
    usable = [c for c in candidates if _length_fits(wanted, c)]
    best = max(
        usable,
        key=lambda c: (*rank(wanted, c), c.explicit == wanted.explicit, c.is_song),
        default=None,
    )
    if best is None or (found := score(wanted, best)) < min_score:
        return None
    return AudioMatch(best, found)


class YouTubeMusicMatcher:
    """Looks tracks up on YouTube Music, a few at a time."""

    def __init__(self, match_videos: bool = True):
        """Search videos too when there is no matching song, if `match_videos`."""
        self.match_videos = match_videos
        self._client = None
        self._slots = asyncio.Semaphore(SEARCH_CONCURRENCY)

    def _search_blocking(self, query: str, kind: str) -> list[dict]:
        """One YouTube Music search. Blocks: it runs in a worker thread."""
        if self._client is None:
            from ytmusicapi import YTMusic

            self._client = YTMusic()
        return self._client.search(
            query, filter=kind, limit=SEARCH_LIMIT, ignore_spelling=True
        )

    async def _search(self, query: str, kind: str) -> list[MatchTrack]:
        """Candidates for a query; "songs" or "videos". [] when it fails twice."""
        async with self._slots:
            for attempt in range(1, SEARCH_ATTEMPTS + 1):
                try:
                    results = await asyncio.to_thread(
                        self._search_blocking, query, kind
                    )
                    return candidates_from(results)
                except Exception as e:
                    # A new client the second time: the first one may be stale.
                    self._client = None
                    logger.debug(
                        "YouTube Music search for %r failed (%s: %s)",
                        query,
                        type(e).__name__,
                        e,
                    )
                    if attempt == SEARCH_ATTEMPTS:
                        return []
                    await asyncio.sleep(1)
        return []

    async def find(
        self, wanted: MatchTrack, isrc: str | None = None
    ) -> AudioMatch | None:
        """The YouTube Music result for a track, or None if there is none."""
        candidates: list[MatchTrack] = []
        if isrc:
            candidates = await self._search(isrc, "songs")
            if (m := select(wanted, candidates)) and m.score >= SURE_SCORE:
                return self._found(wanted, m, "ISRC")

        query = f"{wanted.artist} {wanted.title}".strip()
        candidates += await self._search(query, "songs")
        if m := select(wanted, candidates):
            return self._found(wanted, m, "search")

        if self.match_videos:
            if m := select(wanted, await self._search(query, "videos")):
                return self._found(wanted, m, "video search")
        logger.debug("No YouTube Music match for %s", wanted)
        return None

    @staticmethod
    def _found(wanted: MatchTrack, match: AudioMatch, how: str) -> AudioMatch:
        logger.debug(
            "Matched %s to %s (%s, score %.2f, via %s)",
            wanted,
            match.track,
            match.url,
            match.score,
            how,
        )
        return match
