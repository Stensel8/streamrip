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
