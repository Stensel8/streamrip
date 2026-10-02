"""Catch downloads that came back truncated although no error was raised."""

import logging
import os

import mutagen

logger = logging.getLogger("streamrip")

# Lowest believable bitrate (kbps) for each kind of audio. A truncated file
# keeps the duration its header announces, so its size-over-duration bitrate
# collapses -- usually to a few kbps. The floors sit far below any real
# encoding so that only those collapses are caught.
MIN_KBPS_LOSSY = 50
MIN_KBPS_LOSSLESS = 100  # 16-bit/44.1 kHz FLAC rarely goes under ~400
MIN_KBPS_HIRES = 200

# Under this, header overhead skews the bitrate too much to judge.
MIN_DURATION_S = 5.0

_LOSSLESS_CODECS = ("flac", "alac", "aiff", "wave")


def _min_kbps(audio) -> int:
    """The floor for this file, from what it is rather than what was asked."""
    info = audio.info
    codec = getattr(info, "codec", "") or type(audio).__name__
    bits = getattr(info, "bits_per_sample", 0) or 0
    if not codec.lower().startswith(_LOSSLESS_CODECS):
        return MIN_KBPS_LOSSY
    if bits > 16 or getattr(info, "sample_rate", 0) > 48000:
        return MIN_KBPS_HIRES
    return MIN_KBPS_LOSSLESS


def check_integrity(path: str) -> str | None:
    """Return why the audio file at path looks truncated, or None if it doesn't.

    Only clear failures are reported: an empty file, one mutagen cannot parse,
    or one far smaller than its announced duration requires. A format mutagen
    does not know, or a file without a duration, passes -- there is nothing to
    judge it against.
    """
    size = os.path.getsize(path)
    if size == 0:
        return "the file is empty"

    try:
        audio = mutagen.File(path)
    except Exception as e:
        return f"the file cannot be parsed ({type(e).__name__}: {e})"
    if audio is None:
        logger.debug("Integrity check skipped, unknown format: %s", path)
        return None

    duration = getattr(audio.info, "length", 0) or 0
    if duration < MIN_DURATION_S:
        return None

    kbps = size * 8 / 1000 / duration
    floor = _min_kbps(audio)
    if kbps < floor:
        return (
            f"{size // 1024} KB for {duration:.0f}s is {kbps:.0f} kbps, under "
            f"the {floor} kbps floor for this format: the download is truncated"
        )
    return None
