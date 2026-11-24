"""MusicBrainz (https://musicbrainz.org), the open music database, as a second
opinion for a track a service could not match.

One recording often has several ISRCs (a reissue, another country), and a
service may list it under another than the one a track came with. MusicBrainz
knows them all. It is asked one request a second at most (its rule), only about
the tracks left over, and a failure of it never fails a run.
"""

import logging
import threading
import time

import requests

from .. import __version__
from ..client.audio_match import score
from .models import Track

logger = logging.getLogger("streamrip")

API = "https://musicbrainz.org/ws/2"
# MusicBrainz asks every program to say what it is and where to read about it.
USER_AGENT = f"streamrip/{__version__} (https://opencommit.eu/Stensel8/streamrip)"
INTERVAL = 1.1  # seconds between requests
SURE = 0.9  # how well a recording found by text must match the track
GAP = 5000  # ms: how far a recording's length may be from the track's
LOOKUPS = 3  # recordings looked at for their ISRCs, for a track without one
BUSY_PAUSE = 2.0  # seconds to wait when MusicBrainz is busy and does not say


def _seconds(header: str | None, default: float) -> float:
    try:
        return float(header) if header is not None else default
    except ValueError:
        return default


def _quoted(text: str) -> str:
    """`text` as a phrase of MusicBrainz's search syntax."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _track(recording: dict) -> Track:
    """A MusicBrainz recording as a track to compare with."""
    artists = [
        credit.get("name") or (credit.get("artist") or {}).get("name", "")
        for credit in recording.get("artist-credit", [])
        if isinstance(credit, dict)
    ]
    return Track(
        recording.get("title", ""),
        [a for a in artists if a],
        duration_ms=recording.get("length"),
    )


class MusicBrainz:
    def __init__(
        self,
        session: requests.Session | None = None,
        interval: float = INTERVAL,
        pause: float = BUSY_PAUSE,
    ):
        self.session = session or requests.Session()
        self.interval, self.pause = interval, pause
        self._lock = threading.Lock()  # one request at a time, spaced apart
        self._last = 0.0
        self._cache: dict[tuple, dict] = {}

    def _get(self, path: str, **params: str | int) -> dict:
        """The JSON of a request; {} when MusicBrainz cannot say. A busy
        MusicBrainz (503, 429) is asked once more; only real answers are kept."""
        key = (path, tuple(sorted(params.items())))
        if key in self._cache:
            return self._cache[key]
        body: dict = {}
        with self._lock:
            for attempt in range(2):
                if wait := self._last + self.interval - time.monotonic():
                    time.sleep(max(wait, 0))
                try:
                    resp = self.session.get(
                        f"{API}/{path}",
                        params={**params, "fmt": "json"},
                        headers={
                            "User-Agent": USER_AGENT,
                            "Accept": "application/json",
                        },
                        timeout=15,
                    )
                except requests.RequestException as e:
                    logger.debug("MusicBrainz did not answer: %s", e)
                    return {}
                finally:
                    self._last = time.monotonic()
                if resp.status_code in {429, 503} and attempt == 0:
                    time.sleep(
                        min(_seconds(resp.headers.get("Retry-After"), self.pause), 10)
                    )
                    continue
                if (
                    resp.ok or resp.status_code == 404
                ):  # 404: it does not know that ISRC
                    try:
                        body = resp.json() if resp.ok else {}
                    except ValueError:
                        return {}
                    self._cache[key] = body if isinstance(body, dict) else {}
                    return self._cache[key]
                break
        return {}

    def isrcs_for(self, track: Track) -> tuple[list[str], float]:
        """The other ISRCs of the recording `track` is, and how sure that is.

        By the track's ISRC when it has one (the same ISRC is the same
        recording); else by its title and artist, taking the recording that
        matches best.
        """
        if track.isrc:
            own = track.isrc.upper()
            recordings = self._get(f"isrc/{own}", inc="isrcs").get("recordings", [])
            found = {i.upper() for r in recordings for i in r.get("isrcs", [])}
            return sorted(found - {own}), 0.95
        if not track.title or not track.artists:
            return [], 0.0
        query = f"recording:{_quoted(track.title)} AND artist:{_quoted(track.artist)}"
        if (
            track.duration_ms
        ):  # of the many recordings of a famous song, the ones this long
            query += (
                f" AND dur:[{track.duration_ms - GAP} TO {track.duration_ms + GAP}]"
            )
        hits = self._get("recording", query=query, limit=10).get("recordings", [])
        wanted_live = "live" in track.title.lower()

        def worth(recording: dict) -> bool:
            live = "live" in str(recording.get("disambiguation", "")).lower()
            return score(track, _track(recording)) >= SURE and (wanted_live or not live)

        def closeness(recording: dict) -> tuple[float, int]:
            gap = abs((recording.get("length") or 0) - (track.duration_ms or 0))
            return -score(track, _track(recording)), gap if track.duration_ms else 0

        # Several recordings can match as well; the studio one closest in length
        # first, and the next if MusicBrainz lists no ISRC for it.
        for best in sorted(filter(worth, hits), key=closeness)[:LOOKUPS]:
            recording = self._get(f"recording/{best['id']}", inc="isrcs")
            if isrcs := sorted(i.upper() for i in recording.get("isrcs", [])):
                return isrcs, round(score(track, _track(best)), 2)
        return [], 0.0
