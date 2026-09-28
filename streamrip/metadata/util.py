import functools
from typing import Optional, Type, TypeVar


def get_album_track_ids(source: str, resp) -> list[str]:
    if source == "qobuz" and "tracks" not in resp:
        # Qobuz's album/get stopped inlining "tracks" (July 2026); the client
        # requests extra=track_ids instead.
        return resp["track_ids"]
    tracklist = resp["tracks"]
    if source == "qobuz":
        tracklist = tracklist["items"]
    return [track["id"] for track in tracklist]


def deezer_artists(resp: dict) -> list[str]:
    """The credited artists of a Deezer album or track response."""
    contributors = resp.get("contributors") or []
    names = [c["name"] for c in contributors if c.get("type") == "artist"]
    return names or [safe_get(resp, "artist", "name", default="Unknown Artist")]


def safe_get(dictionary, *keys, default=None):
    return functools.reduce(
        lambda d, key: d.get(key, default) if isinstance(d, dict) else default,
        keys,
        dictionary,
    )


T = TypeVar("T")


def typed(thing, expected_type: Type[T]) -> T:
    assert isinstance(thing, expected_type)
    return thing


# Tidal's audioQuality values mapped to streamrip's quality ids. HI_RES was MQA
# (retired by Tidal); HI_RES_LOSSLESS is 24-bit FLAC.
TIDAL_QUALITY_IDS: dict[str, int] = {
    "LOW": 0,
    "HIGH": 1,
    "LOSSLESS": 2,
    "HI_RES": 3,
    "HI_RES_LOSSLESS": 3,
}


def tidal_quality_id(audio_quality: str | None) -> int:
    """Quality id for a Tidal audioQuality value, tolerating unknown ones."""
    if audio_quality is None:
        return 0
    return TIDAL_QUALITY_IDS.get(audio_quality, 2)


def get_quality_id(
    bit_depth: Optional[int],
    sampling_rate: Optional[int | float],
) -> int:
    """Get the universal quality id from bit depth and sampling rate.

    :param bit_depth:
    :type bit_depth: Optional[int]
    :param sampling_rate: In kHz
    :type sampling_rate: Optional[int]
    """
    # XXX: Should `0` quality be supported?
    if bit_depth is None or sampling_rate is None:  # is lossy
        return 1

    if bit_depth == 16:
        return 2

    if bit_depth == 24:
        if sampling_rate <= 96:
            return 3

        return 4

    raise Exception(f"Invalid {bit_depth = }")
