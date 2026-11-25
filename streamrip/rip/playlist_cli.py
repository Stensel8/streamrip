"""`streamrip playlist`: move liked songs and playlists between Spotify, Tidal and
CSV files, or download them from a lossless source.

What Music-Sync's `music-sync` command did, as part of streamrip. The work
itself is in streamrip/sync; this is the command line around it.
"""

import logging
import shutil
import sys
import time
from functools import wraps
from pathlib import Path
lazy from collections.abc import Callable

import click

from .options import finish, previewable, sources_callback
lazy from ..config import Config
lazy from ..console import console, err_console, plural
lazy from ..sync.csvio import read_tracks, write_tracks
lazy from ..sync.engine import (
    LIKED,
    Miss,
    Progress,
    Step,
    import_albums,
    import_tracks,
    remaining,
    select_tracks,
)
lazy from ..sync.errors import SyncError
lazy from ..sync.export import export_lists
lazy from ..sync.models import Track
lazy from ..sync.providers.base import Provider
lazy from ..sync.report import write_report
lazy from ..sync.services import (
    LABELS,
    Services,
)
lazy from .cli import _ask_source, coro, main_session

logger = logging.getLogger("streamrip")

SERVICE = click.Choice(["spotify", "tidal"], case_sensitive=False)


def _warn(message: str):
    err_console.print(message, style="yellow", markup=False)


def _seconds(seconds: float) -> str:
    """A rough duration for people: "40 s", "3 min"."""
    if seconds < 60:
        return f"{max(round(seconds / 10) * 10, 10)} s"
    return f"{round(seconds / 60)} min"


class _ProgressLine:
    """Progress on stderr. On a terminal: one line per step of the work, redrawn
    in place. In a log file: a line per 50 tracks, and one when a step is
    complete."""

    def __init__(self):
        self.tty = sys.stderr.isatty()
        self.text = ""  # the step the current line is about
        self.complete = True  # the last step reported was complete
        self.started = time.monotonic()

    def __call__(self, step: Step):
        if step.text != self.text or self.complete:
            if self.tty and not self.complete:
                print(file=sys.stderr, flush=True)  # end the line left open
            self.text, self.started = step.text, time.monotonic()
        self.complete = step.total is not None and step.done >= step.total
        if self.tty:
            width = max(shutil.get_terminal_size().columns - 1, 40)
            end = "\n" if self.complete else ""
            line = self.line(step)[:width]
            print(f"\r{line:<{width}}", end=end, file=sys.stderr, flush=True)
        elif self.complete or (step.done and step.done % 50 == 0):
            print(self.line(step, current=False), file=sys.stderr, flush=True)

    def line(self, step: Step, *, current: bool = True) -> str:
        """ "Finding the tracks on Tidal  [#####---]  45/300  40 found, 5 not
        found  1 min left  Artist - Title" """
        if step.total is None:
            parts = [step.text, f"{step.done} so far"]
        else:
            filled = round(20 * step.done / step.total) if step.total else 20
            bar = f"[{'#' * filled}{'-' * (20 - filled)}]"
            parts = [step.text, bar, f"{step.done}/{step.total}"]
        if step.phase == "match":
            parts.append(f"{step.found} found, {step.done - step.found} not found")
        elapsed = time.monotonic() - self.started
        if (left := remaining(step.done, step.total, elapsed)) is not None:
            parts.append(f"{_seconds(left)} left")
        if current and step.track and not self.complete:
            parts.append(str(step.track))
        return "  ".join(parts)


def _progress(quiet: bool) -> Progress | None:
    return None if quiet else _ProgressLine()


def _write_report(path: Path | None, matched: list, misses: list[Miss]):
    """Save the audit trail of a transfer or import, if it was asked for."""
    if path:
        rows = write_report(path, matched, misses)
        console.print(f"{plural(rows, 'track')} reported in {path}")


def _report_unmatched(misses: list[Miss], path: Path | None):
    """Show the tracks that were not found and why, or save them all."""
    if not misses:
        return
    if path:
        write_tracks(path, [miss.track for miss in misses])
        console.print(
            f"{plural(len(misses), 'track')} without a match written to {path}"
        )
        return
    console.print("\nNot found:")
    for miss in misses[:10]:
        console.print(f"  {miss.track}: {miss.why}", markup=False, highlight=False)
    if len(misses) > 10:
        console.print(
            f"  ... and {len(misses) - 10} more (use --unmatched FILE to save them all)"
        )


def _config(ctx: click.Context) -> Config:
    """The config, or exit 1 when it did not load (the root command said why)."""
    config = ctx.find_root().obj["config"]
    if config is None:
        ctx.exit(1)
    return config


def _services(ctx: click.Context) -> Services:
    return Services(_config(ctx))


def _explained(command: Callable) -> Callable:
    """Turn the failures the playlist tools can explain into a plain error
    message and exit code 1, instead of a traceback."""

    @wraps(command)
    def wrapper(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except (SyncError, OSError) as e:
            raise click.ClickException(str(e)) from e

    return wrapper


def _selection(liked: bool, playlist: str | None, everything: bool, *extra: bool):
    """Exactly one of --liked, --playlist and --all (and the extra flags)."""
    if sum(map(bool, (liked, playlist, everything, *extra))) != 1:
        raise click.UsageError("Choose one of --liked, --playlist NAME and --all.")


def _select(
    provider: Provider,
    liked: bool,
    playlist: str | None,
    progress: Progress | None,
) -> list[tuple[str, list[Track]]]:
    """The (name, tracks) lists asked for: liked songs, a playlist, or all."""
    return select_tracks(
        provider, liked=liked, playlist=playlist, on_skip=_warn, progress=progress
    )


def selection_options(command: Callable) -> Callable:
    """--liked, --playlist and --all: which tracks to work on."""
    for option in reversed(
        [
            click.option("--liked", is_flag=True, help="Your liked / saved songs."),
            click.option(
                "--playlist", metavar="NAME_OR_ID", help="One playlist, by name or id."
            ),
            click.option(
                "--all",
                "everything",
                is_flag=True,
                help="Liked songs and every playlist you can read.",
            ),
        ]
    ):
        command = option(command)
    return command


def matching_options(command: Callable) -> Callable:
    """How sure a match must be, and what to do with the tracks not found."""
    for option in reversed(
        [
            click.option(
                "--dry-run",
                is_flag=True,
                help="Look everything up, but change nothing.",
            ),
            click.option(
                "--min-score",
                type=click.FloatRange(0, 1),
                default=0.8,
                show_default=True,
                help="How sure a text match must be (0 to 1).",
            ),
            click.option(
                "--unmatched",
                type=click.Path(dir_okay=False, path_type=Path),
                help="Write the tracks that were not found to this CSV.",
            ),
            click.option(
                "--report",
                type=click.Path(dir_okay=False, path_type=Path),
                help="Write every track with what it was matched to, how, and "
                "how sure, to this CSV.",
            ),
            click.option("-q", "--quiet", is_flag=True, help="No progress output."),
        ]
    ):
        command = option(command)
    return command


@click.group()
def playlist():
    """Move liked songs and playlists between Spotify, Tidal and CSV files,
    or download them from a lossless source.

    Tracks are found by their ISRC, or else by title, artist and length, and a
    live version is never taken for the studio one. Running a transfer twice
    adds nothing twice. docs/playlists.md has the setup and the details.

    \b
    Examples:
        streamrip login spotify
        streamrip playlist transfer spotify tidal --liked
        streamrip playlist export spotify --all -o backup/
        streamrip playlist import tidal liked.csv --playlist "From CSV"
        streamrip playlist download spotify --playlist "Road trip" --source qobuz
    """


# --- working with the lists ------------------------------------------------------


@playlist.command("list")
@click.argument("service", type=SERVICE)
@click.pass_context
@_explained
def list_playlists(ctx, service):
    """List your playlists, with their ids."""
    for info in _services(ctx).provider(service).playlists():
        tracks = "?" if info.track_count is None else info.track_count
        note = "" if info.readable else "  (not yours: its tracks cannot be read)"
        console.print(
            f"{info.id}  {tracks:>5}  {info.name}{note}", markup=False, highlight=False
        )


@playlist.command()
@click.argument("service", type=SERVICE)
@selection_options
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="The file (a folder with --all).",
)
@click.option(
    "--format",
    "file_format",
    type=click.Choice(["csv", "m3u8"]),
    default="csv",
    show_default=True,
    help="CSV, or an M3U8 playlist for a player or server.",
)
@click.option(
    "--library",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="m3u8: the music folder whose files the playlists point at.",
)
@click.option(
    "--absolute-paths",
    is_flag=True,
    help="m3u8: absolute paths, not relative to the playlist file.",
)
@click.option(
    "--unmatched",
    type=click.Path(dir_okay=False, path_type=Path),
    help="m3u8: write the tracks not in the library to this CSV.",
)
@click.option("-q", "--quiet", is_flag=True, help="No progress output.")
@click.pass_context
@_explained
def export(
    ctx,
    service,
    liked,
    playlist,
    everything,
    output,
    file_format,
    library,
    absolute_paths,
    unmatched,
    quiet,
):
    """Save liked songs or playlists as CSV or M3U8 files.

    A CSV reads back in with `streamrip playlist import` and `streamrip csv`,
    and in Exportify's and most other tools' format. An M3U8 playlist is for a
    player or server (Navidrome, Jellyfin, Music Assistant, ...): with
    --library each entry is a file of your music folder, found by its ISRC or
    else by title, artist and length; what the folder lacks is left out.

    \b
        streamrip playlist export tidal --all --format m3u8 --library ~/Music -o ~/Music/Playlists
    """
    _selection(liked, playlist, everything)
    m3u8 = file_format == "m3u8"
    if not m3u8 and (library or absolute_paths or unmatched):
        raise click.UsageError(
            "--library, --absolute-paths and --unmatched go with --format m3u8."
        )
    provider = _services(ctx).provider(service)
    selected = _select(provider, liked, playlist, _progress(quiet))

    def say(line: str):
        if line.startswith("No library"):
            _warn(line)
        elif not quiet or line[0].isdigit():
            console.print(line, highlight=False)

    lines, missing = export_lists(
        selected,
        everything=everything,
        output=output,
        service=service,
        m3u8=m3u8,
        library=library,
        absolute=absolute_paths,
        say=say,
    )
    for line in lines:
        console.print(line, highlight=False)
    if missing and unmatched:
        write_tracks(unmatched, missing)
        console.print(
            f"{plural(len(missing), 'track')} not in the library written to {unmatched}",
            highlight=False,
        )


@playlist.command("import")
@click.argument("service", type=SERVICE)
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--playlist",
    default="streamrip import",
    show_default=True,
    help="The playlist to add to; made if it is not there.",
)
@click.option(
    "--to-favorites", is_flag=True, help="Add to your favourites (liked songs) instead."
)
@click.option(
    "--albums",
    is_flag=True,
    help="The rows are albums (artist,album): add them to your favourite albums.",
)
@matching_options
@click.pass_context
@_explained
def import_csv(
    ctx,
    service,
    file,
    playlist,
    to_favorites,
    albums,
    dry_run,
    min_score,
    unmatched,
    report,
    quiet,
):
    """Add the tracks (or albums) of a CSV file to a playlist or your favourites.

    What is already there is skipped. The columns of `streamrip playlist
    export`, Exportify and most other tools are understood; a file without a
    header row is read as "artist,title".
    """
    if to_favorites and albums:
        raise click.UsageError("--albums always adds to your favourite albums.")
    tracks = read_tracks(file)
    if not tracks:
        raise click.ClickException(f"No tracks found in {file}.")
    services = _services(ctx)
    provider, progress = services.provider(service), _progress(quiet)
    if albums:  # a row per album, as csv2tidal took them
        result = import_albums(
            provider, tracks, min_score=min_score, dry_run=dry_run, progress=progress
        )
    else:
        result = import_tracks(
            provider,
            tracks,
            None if to_favorites else playlist,
            min_score=min_score,
            dry_run=dry_run,
            progress=progress,
            musicbrainz=services.musicbrainz(),
        )
    console.print(result.summary(), markup=False, highlight=False)
    _report_unmatched(result.misses, unmatched)
    _write_report(report, result.matched, result.misses)


@playlist.command()
@click.argument("source", type=SERVICE)
@click.argument("target", type=SERVICE)
@selection_options
@click.option(
    "--sync-favorites",
    is_flag=True,
    help="Your liked songs, to the target's favourites.",
)
@click.option(
    "--to-playlist",
    metavar="NAME",
    help="The playlist to fill (default: the same name as the source's).",
)
@matching_options
@click.pass_context
@_explained
def transfer(
    ctx,
    source,
    target,
    liked,
    playlist,
    everything,
    sync_favorites,
    to_playlist,
    dry_run,
    min_score,
    unmatched,
    report,
    quiet,
):
    """Copy liked songs or playlists from one service to the other.

    What the target already has is skipped, so a transfer can be run again to
    bring it up to date.
    """
    _selection(liked, playlist, everything, sync_favorites)
    if source == target:
        raise click.UsageError("The source and the target are the same service.")
    if to_playlist and (everything or sync_favorites):
        raise click.UsageError(
            "--to-playlist cannot go with --all or --sync-favorites."
        )
    services = _services(ctx)
    source_provider, target_provider = (
        services.provider(source),
        services.provider(target),
    )
    progress, musicbrainz = _progress(quiet), services.musicbrainz()
    misses: list[Miss] = []
    matched: list = []
    selected = _select(source_provider, liked or sync_favorites, playlist, progress)
    for name, tracks in selected:
        # None: the favourites (liked songs) of the target.
        if sync_favorites:
            destination = None
        elif to_playlist:
            destination = to_playlist
        else:
            destination = f"{LIKED} (from {LABELS[source]})" if name == LIKED else name
        result = import_tracks(
            target_provider,
            tracks,
            destination,
            min_score=min_score,
            dry_run=dry_run,
            progress=progress,
            musicbrainz=musicbrainz,
        )
        console.print(result.summary(), markup=False, highlight=False)
        misses.extend(result.misses)
        matched.extend(result.matched)
    _report_unmatched(misses, unmatched)
    _write_report(report, matched, misses)


@playlist.command()
@click.argument("service", type=SERVICE)
@selection_options
@click.option(
    "-s",
    "--source",
    type=click.Choice(["qobuz", "tidal", "deezer", "soundcloud", "spotify"]),
    help="Where to download them from. Asked when left out.",
)
@click.option(
    "-fs",
    "--fallback-source",
    callback=sources_callback,
    help="Where to look for a track the first source does not have. Several, "
    "comma separated, are tried in turn.",
)
@click.option("-q", "--quiet", is_flag=True, help="No progress output while reading.")
@previewable
@click.pass_context
def download(
    ctx, service, liked, playlist, everything, source, fallback_source, quiet, preview
):
    """Download liked songs or playlists from another source.

    Reads the list from Spotify or Tidal and finds each track on the source you
    pick, the way `streamrip csv` does: a Spotify playlist in lossless from
    Qobuz, Tidal or Deezer, say, where a Spotify link is recorded in real time or matched on YouTube Music.
    Each list goes in a folder of its own.
    """
    _selection(liked, playlist, everything)
    try:
        provider = _services(ctx).provider(service)
        selected = _select(provider, liked, playlist, _progress(quiet))
    except (SyncError, OSError) as e:
        raise click.ClickException(str(e)) from e
    if source is None:
        source = _ask_source(sum(len(tracks) for _, tracks in selected))

    async def rip():
        async with main_session(ctx) as main:
            for name, tracks in selected:
                await main.resolve_csv(name, tracks, source, fallback_source)
            await finish(main, preview)

    # The download commands' own handling: Ctrl-C and SIGTERM stop it cleanly,
    # a missing ffmpeg or a lapsed login is a message and exit code 1.
    coro(rip)()


# --- checking the setup -----------------------------------------------------------
