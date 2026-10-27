"""Locate and run FFmpeg."""

import asyncio
import functools
import shutil
lazy from pathlib import Path

from ..exceptions import FFmpegNotFoundError, NonStreamableError


@functools.cache
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
    """What to tell someone who runs streamrip without an ffmpeg."""
    return (
        "No ffmpeg installation found on your system. streamrip needs ffmpeg "
        "(for Tidal hi-res, SoundCloud, Spotify's audio and conversion).\n"
        "Install ffmpeg:\n"
        "  Linux:    apt install ffmpeg\n"
        "  macOS:    brew install ffmpeg\n"
        "  Windows:  winget install ffmpeg\n"
        "or run streamrip's installer again: it brings an ffmpeg of its own."
    )


def require_ffmpeg() -> str:
    """The path to ffmpeg, or FFmpegNotFoundError saying how to get one."""
    if (path := find_ffmpeg()) is None:
        raise FFmpegNotFoundError(ffmpeg_missing_message())
    return path


async def run_ffmpeg(
    *args: str | Path,
    output: Path,
    failure: str,
    error: type[Exception] = NonStreamableError,
) -> None:
    """Run ffmpeg with args to write output: all of it, or nothing.

    Only errors are printed (anything more would garble the progress bars),
    and they are kept: a failure raises `error` as "<failure> (exit N): <the
    end of what ffmpeg said>". A failure, an output that never appeared, or
    a cancellation kills and reaps the process and removes output, so no
    half-written file stays behind and nothing goes on writing to one.
    """
    proc = await asyncio.create_subprocess_exec(
        require_ffmpeg(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *args,
        output,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await proc.communicate()
        if proc.returncode or not output.is_file():
            said = stderr.decode(errors="replace")[-300:].strip()
            raise error(f"{failure} (exit {proc.returncode}): {said}")
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        output.unlink(missing_ok=True)
        raise
