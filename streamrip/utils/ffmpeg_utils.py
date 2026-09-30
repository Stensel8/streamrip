"""Locate an FFmpeg executable."""

import functools
import shutil


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Return a path to an ffmpeg executable, or None if none is available.

    Prefers an ffmpeg already on PATH. Falls back to the binary bundled by
    the optional `ffmpeg` extra (imageio-ffmpeg), so streamrip works without
    a separate, OS-specific ffmpeg install.
    """
    path = shutil.which("ffmpeg")
    if path is not None:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_missing_message() -> str:
    """What to tell someone whose Tidal hi-res download has no ffmpeg."""
    return (
        "No ffmpeg installation found on your system, and Tidal hi-res "
        "downloads need it.\n"
        "Install ffmpeg:\n"
        "  Linux:    apt install ffmpeg\n"
        "  macOS:    brew install ffmpeg\n"
        "  Windows:  winget install ffmpeg\n"
        "or proceed with the built-in ffmpeg (a Python package) by running, in "
        "the environment streamrip is installed in:\n"
        "  pip install imageio-ffmpeg\n"
        "  (pipx: pipx inject streamrip imageio-ffmpeg)\n"
        "or skip hi-res by running streamrip with -q 2 (16-bit FLAC), or by "
        "setting quality = 2 under [tidal] in the config."
    )
