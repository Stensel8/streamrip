"""Options and steps the download commands share, for the command line's
`url`, `file`, `id`, `search`, `csv`, `lastfm` and `playlist download`."""

import click

lazy from .main import fallback_chain
lazy from .plan import show_plan

# The sources, and what a download from each gives: shown when asked.
SOURCES = frozendict(
    {
        "qobuz": "lossless, up to 24-bit",
        "tidal": "lossless, hi-res where there is some",
        "deezer": "FLAC or MP3, as your subscription allows",
        "spotify": "its own audio (Soloist, Linux), else AAC from YouTube Music",
        "soundcloud": "MP3, and only uploads by the artists themselves",
    }
)


def previewable(command):
    """Gives a download command --preview: look everything up like a download
    does, and say what it would do, without downloading."""
    return click.option(
        "--preview",
        is_flag=True,
        help="Say what would be downloaded (quality, new tracks, time at the "
        "pace), and download nothing.",
    )(command)


async def finish(main, preview: bool):
    """Download what was resolved, or only say what that would be."""
    if preview:
        show_plan(main)
    elif not await main.rip():
        raise SystemExit(1)  # so a script or cron job sees that tracks failed


def sources_callback(ctx, param, value: str | None) -> str | None:
    """A list of sources, "tidal,deezer", each of which must be one."""
    unknown = [s for s in fallback_chain(value, "") if s not in SOURCES]
    if unknown:
        raise click.BadParameter(
            f"{', '.join(unknown)}: choose from {', '.join(SOURCES)}."
        )
    return value
