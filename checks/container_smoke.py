"""What streamrip asks of the image's small ffmpeg, in the image itself.

The image's ffmpeg has only the filters, encoders and demuxers streamrip was
thought to need (see the Containerfile), and no ffprobe, shell or sound tools:
a feature that needs more fails only there, so `--version` says nothing. Run
it with the image's own python, from a checkout (the script goes in by stdin:
a bind mount fails on a runner whose docker daemon is somewhere else):

    docker run --rm -i --entrypoint /opt/streamrip/bin/python IMAGE - < checks/container_smoke.py

Exit code 1, and what failed, if anything did.
"""

import asyncio
import math
import struct
import sys
import tempfile
import wave
from pathlib import Path

from streamrip import converter
from streamrip.utils.ffmpeg_utils import measure_replaygain

CODECS = ("FLAC", "ALAC", "AIFF", "MP3", "OPUS", "VORBIS", "AAC")


def write_sine(path: Path, seconds: int = 3, rate: int = 44100) -> None:
    """A stereo 16-bit WAV of a 440 Hz tone, made with nothing but the standard library."""
    frames = b"".join(
        struct.pack("<hh", s, s)
        for s in (
            int(12000 * math.sin(2 * math.pi * 440 * n / rate))
            for n in range(seconds * rate)
        )
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)


async def main() -> list[str]:
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "tone.wav"
        write_sine(source)

        measured = await measure_replaygain(source)
        if measured is None:
            problems.append("ReplayGain could not be measured (ffmpeg's ebur128)")
        elif not -30 < measured[0] < 30:
            problems.append(f"ReplayGain {measured[0]:+.1f} dB is not believable")

        for codec in CODECS:
            work = Path(tmp) / codec
            work.mkdir()
            copy = work / "tone.wav"
            copy.write_bytes(source.read_bytes())
            engine_class = converter.get(codec)
            try:
                engine = engine_class(
                    filename=copy,
                    ffmpeg_arg=None
                    if engine_class.lossless
                    else engine_class.get_quality_arg(192),
                    sampling_rate=44100,
                    bit_depth=16,
                    remove_source=True,
                )
                await engine.convert()
                if engine.final_fn.stat().st_size < 1000:
                    problems.append(f"{codec}: the converted file is empty")
            except Exception as e:
                problems.append(f"{codec}: {type(e).__name__}: {str(e)[-160:]}")
    return problems


if __name__ == "__main__":
    found = asyncio.run(main())
    for line in found:
        print("FAIL", line)
    if not found:
        print(f"ok: ReplayGain and {len(CODECS)} codecs")
    sys.exit(1 if found else 0)
