from __future__ import annotations

import logging
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from .util import tidal_quality_id

logger = logging.getLogger("streamrip")

# Tidal's catalog is inconsistent about which bracket style tags an edition
# name, so square and round brackets are folded together before matching --
# otherwise the same release under each style looks distinct.
_BRACKETS = str.maketrans("[]", "()")


def _norm(text: str | None) -> str:
    return (text or "").strip().lower().translate(_BRACKETS)


def _artist_ids(album: dict) -> frozenset:
    """Ids of every artist an album is credited to (Qobuz and Tidal shape)."""
    ids = {a.get("id") for a in album.get("artists") or []}
    if isinstance(main := album.get("artist"), dict):
        ids.add(main.get("id"))
    ids.discard(None)
    return frozenset(ids)


def dedup_releases(
    albums: list[dict],
    key: Callable[[dict], Hashable],
    rank: Callable[[dict], tuple],
) -> list[dict]:
    """Keep only the best copy of a release listed more than once.

    Services list one release under an artist more than once: a clean and an
    explicit master, or the same master at two quality tiers. Albums with the
    same key -- same artists, title, edition and track count, as good as
    certain to be the same release rather than two that share a title -- are
    kept once, as the copy that ranks highest.
    """
    groups: dict[Hashable, list[dict]] = {}
    for album in albums:
        groups.setdefault(key(album), []).append(album)
    return [max(group, key=rank) for group in groups.values()]


def _qobuz_albums(resp: dict, prefer_explicit: bool) -> list[dict]:
    albums = resp["albums"]["items"]
    # Qobuz lists every release of a song the artist is credited on in any
    # role -- other artists' covers and remixes included (18 of 30 Seconds To
    # Mars' 56). Keep the ones the artist is one of the album artists of,
    # which still keeps collaborations.
    me = resp.get("id")
    own = [a for a in albums if me in _artist_ids(a) or not _artist_ids(a)]
    logger.debug(
        "%s: left out %d release(s) by other artists",
        resp["name"],
        len(albums) - len(own),
    )
    if not prefer_explicit:
        return own
    return dedup_releases(
        own,
        key=lambda a: (
            _artist_ids(a),
            _norm(a.get("title")),
            _norm(a.get("version")),
            a.get("tracks_count"),
        ),
        rank=lambda a: (
            bool(a.get("parental_warning")),
            a.get("maximum_bit_depth") or 0,
            a.get("maximum_sampling_rate") or 0,
        ),
    )


def _tidal_albums(resp: dict, prefer_explicit: bool) -> list[dict]:
    albums = resp["albums"]
    if not prefer_explicit:
        return albums
    return dedup_releases(
        albums,
        key=lambda a: (
            _artist_ids(a),
            _norm(a.get("title")),
            a.get("numberOfTracks", 0),
        ),
        rank=lambda a: (
            bool(a.get("explicit")),
            tidal_quality_id(a.get("audioQuality")),
        ),
    )


@dataclass(slots=True)
class ArtistMetadata:
    name: str
    ids: list[str]

    def album_ids(self):
        return self.ids

    @classmethod
    def from_resp(
        cls, resp: dict, source: str, prefer_explicit: bool = True
    ) -> ArtistMetadata:
        """The artist's own releases, each listed once.

        With prefer_explicit, a release listed twice keeps its explicit
        (then highest-quality) copy; without it, every listing is kept.
        """
        logger.debug(resp)
        if source == "qobuz":
            albums = _qobuz_albums(resp, prefer_explicit)
        elif source == "tidal":
            albums = _tidal_albums(resp, prefer_explicit)
        elif source == "deezer":
            # Deezer's album list carries no track counts to tell a duplicate
            # listing from a single that shares an album's title.
            albums = resp["albums"]
        else:
            raise NotImplementedError
        return cls(resp["name"], [a["id"] for a in albums])
