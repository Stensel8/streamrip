import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager, closing
from functools import wraps
from typing import Any

import aiofiles
import aiohttp
import click
from click_help_colors import HelpColorsGroup  # type: ignore
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.markup import escape
from rich.traceback import install

from .. import __version__, db
from ..client import new_session
from ..config import DEFAULT_CONFIG_PATH, Config, set_user_defaults
from ..console import console
from ..exceptions import FFmpegNotFoundError
from ..media.csv_playlist import read_tracks
from ..utils.ssl_utils import print_ssl_error_help
from .interactive import Confirm, Prompt
from .main import Main

logger = logging.getLogger("streamrip")


# Where this build of streamrip comes from, for update checks and advice.
REPOSITORY = "Stensel8/streamrip"
GIT_DOWNLOADS = "https://git-scm.com/downloads"


def _upgrade_command(version: str, *, is_release: bool = True) -> str:
    """Install the detected release tag or the default branch used as fallback.

    The command installs from a git URL, so it needs git (pip cannot do
    without it; uv sometimes can).
    """
    ref = f"v{version}" if is_release else "HEAD"
    target = f"git+https://github.com/{REPOSITORY}.git@{ref}"
    uv = shutil.which("uv")
    if uv:
        return f'"{uv}" pip install --python "{sys.executable}" --upgrade {target}'
    return f'"{sys.executable}" -m pip install --upgrade {target}'


def coro(f):
    """Adapt an async CLI command to Click with shared error handling."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        """Run the command, report handled errors, and exit 1 for missing ffmpeg."""

        async def run():
            """Run the command with SIGINT cancellation where supported."""
            # Ctrl-C used to be ignored until whatever was in flight finished,
            # so people force-killed streamrip -- which skips the cleanup in
            # Main.__aexit__ and leaves __artwork directories behind. Cancel
            # the task instead so everything unwinds (upstream PR #1025).
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()

            def stop():
                """Cancel the active task and restore default SIGINT handling."""
                console.print("\n[yellow]Stopping... (Ctrl-C again to force)")
                loop.remove_signal_handler(signal.SIGINT)
                if task is not None:
                    task.cancel()

            try:
                loop.add_signal_handler(signal.SIGINT, stop)
            except NotImplementedError, RuntimeError:
                pass  # Windows: default KeyboardInterrupt behaviour

            return await f(*args, **kwargs)

        try:
            return asyncio.run(run())
        except asyncio.CancelledError, KeyboardInterrupt:
            console.print("[yellow]Stopped.")
        except aiohttp.ClientConnectorCertificateError as e:
            console.print(f"[red]SSL Certificate verification error: {e}[/red]")
            print_ssl_error_help()
        except FFmpegNotFoundError as e:
            console.print(escape(str(e)), style="red")
            sys.exit(1)

    return wrapper


@asynccontextmanager
async def main_session(ctx):
    """Shared by every download command (url, file, search, lastfm, id).

    Opens the config as a session and runs the caller's block. The update
    check lives in `_StreamripGroup.main()` so it also covers Click's
    `--help`, `--version`, and bare-invocation paths.
    """
    with ctx.obj["config"] as cfg:
        cfg: Config
        async with Main(cfg) as main:
            yield main


def _require_config(ctx) -> Config:
    """The config of a command that cannot run without one.

    `rip()` leaves it None when the file does not load, so that `config reset`
    and `config path` still work to fix it, and has already said why. Every
    other command ends here with exit code 1, so a script or cron job sees
    the failure instead of a run that quietly did nothing.
    """
    cfg: Config | None = ctx.obj["config"]
    if cfg is None:
        ctx.exit(1)
    return cfg


def _git_note() -> str:
    """Tell the user the update command needs git, and warn if it is not there."""
    if shutil.which("git"):
        return "[dim]This needs git.[/dim]"
    return (
        "[yellow]This needs git, which was not found on your PATH. "
        f"Install it first: {GIT_DOWNLOADS}[/yellow]"
    )


def _print_update_notice(
    latest_version: str, notes: str | None, is_release: bool
) -> None:
    """Print the available-update message and its release notes, if any."""
    console.print(
        f"[green]A new version of streamrip [cyan]v{latest_version}"
        f"[/cyan] is available.[/green]\n"
        f"This installation is in [cyan]{escape(sys.prefix)}[/cyan]. Update it with:\n"
        f"[white][bold]"
        f"{_upgrade_command(latest_version, is_release=is_release)}"
        "[/bold][/white]\n"
        f"{_git_note()}\n"
    )
    if notes:
        console.print(Markdown(notes))


def _config_path_from(command_line: list[str] | None) -> str:
    """The --config-path of a command line, or the default.

    The update check runs before Click has parsed anything, so the config it
    reads has to be found here.
    """
    args = sys.argv[1:] if command_line is None else list(command_line)
    for i, arg in enumerate(args):
        if arg == "--config-path" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--config-path="):
            return arg.partition("=")[2]
    return DEFAULT_CONFIG_PATH


def _update_check_disabled(config_path: str) -> bool:
    """Whether the config at config_path has `no_update_check = true` in [cli].

    It reads that one key and nothing else. A config that cannot be read
    leaves the check on: `rip()` is what reports a broken config.
    """
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f).get("cli", {}).get("no_update_check") is True
    except OSError, tomllib.TOMLDecodeError:
        return False


class _StreamripGroup(HelpColorsGroup):
    """Checks for updates before literally anything else runs.

    Click short-circuits --help, --version, and a bare invocation with no
    subcommand before the group's own callback (`rip()` below) ever runs --
    so a check placed there misses all three. `main()` is Click's actual
    entry point, called before any of that, for every invocation alike:
    one check covers all of them (asked for explicitly, 2026-09-30). Only
    `no_update_check = true` in the config turns it off, which is why `main()`
    reads that key itself.
    """

    def main(self, *args, **kwargs):
        """Check for updates, run the command, then print a notice if newer."""
        notice = None
        command_line = args[0] if args else kwargs.get("args")
        if not _update_check_disabled(_config_path_from(command_line)):
            try:
                with console.status(
                    "streamrip: Checking for updates...", spinner="dots"
                ):
                    # Close the coroutine even if run() rejects an active event loop.
                    with closing(latest_streamrip_version()) as check:
                        latest_version, notes, is_release = asyncio.run(check)
                if is_newer_version(latest_version):
                    notice = (latest_version, notes, is_release)
            except Exception as exc:
                logger.debug("Could not check for updates: %s", exc)
        try:
            return super().main(*args, **kwargs)
        finally:
            # In a finally, not just after: search's screen-clearing picker
            # would wipe a notice printed too early, and a raised/cancelled
            # command must not also cancel the one thing this was for.
            if notice is not None:
                _print_update_notice(*notice)


@click.group(
    # The command is `streamrip`, whatever this function is called: Click shows
    # the name in every usage line and error ("Try 'streamrip csv --help'").
    name="streamrip",
    cls=_StreamripGroup,
    help_headers_color="yellow",
    help_options_color="green",
)
@click.version_option(version=__version__)
@click.option(
    "--config-path",
    default=DEFAULT_CONFIG_PATH,
    help="Path to the configuration file",
    type=click.Path(readable=True, writable=True),
)
@click.option(
    "-f",
    "--folder",
    help="The folder to download items into.",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option(
    "-ndb",
    "--no-db",
    help="Download items even if they have been logged in the database",
    default=False,
    is_flag=True,
)
@click.option(
    "-q",
    "--quality",
    help="The maximum quality allowed to download",
    type=click.IntRange(min=0, max=4),
)
@click.option(
    "-c",
    "--codec",
    help="Convert the downloaded files to an audio codec "
    "(ALAC, FLAC, AIFF, MP3, AAC, OGG, or OPUS)",
    type=click.Choice(
        ["ALAC", "FLAC", "AIFF", "MP3", "AAC", "OGG", "OPUS"], case_sensitive=False
    ),
)
@click.option(
    "--no-progress",
    help="Do not show progress bars",
    is_flag=True,
    default=False,
)
@click.option(
    "--no-ssl-verify",
    help="Disable SSL certificate verification (use if you encounter SSL errors)",
    is_flag=True,
    default=False,
)
@click.option(
    "-v",
    "--verbose",
    help="Enable verbose output (debug mode)",
    is_flag=True,
)
@click.pass_context
def rip(
    ctx, config_path, folder, no_db, quality, codec, no_progress, no_ssl_verify, verbose
):
    """A fast, all-in-one scriptable music downloader for Qobuz, Deezer, Tidal, SoundCloud, and Spotify."""
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console)],
    )
    if verbose:
        # No show_locals: a traceback is what people paste into bug reports,
        # and the locals of a failing frame routinely hold credentials -- the
        # Deezer ARL in the login frames, and every Config repr, which includes
        # all of them. Masking by variable name could not catch the reprs.
        install(console=console, suppress=[click])
        logger.setLevel(logging.DEBUG)
        logger.debug("Showing all debug logs")
    else:
        install(console=console, suppress=[click, asyncio], max_frames=1)
        logger.setLevel(logging.INFO)

    if not os.path.isfile(config_path):
        console.print(
            f"No file found at [bold cyan]{config_path}[/bold cyan], creating default config.",
        )
        set_user_defaults(config_path)

    # pass to subcommands
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path

    try:
        c = Config(config_path)
    except Exception as e:
        console.print(
            f"Error loading config from [bold cyan]{config_path}[/bold cyan]: {e}\n"
            "Try running [bold]streamrip config reset[/bold]",
        )
        ctx.obj["config"] = None
        return

    # set session config values to command line args
    if no_db:
        c.session.database.downloads_enabled = False
    if folder is not None:
        c.session.downloads.folder = folder

    if quality is not None:
        c.session.qobuz.quality = quality
        c.session.tidal.quality = quality
        c.session.deezer.quality = quality

    if codec is not None:
        c.session.conversion.enabled = True
        c.session.conversion.codec = codec.upper()

    if no_progress:
        c.session.cli.progress_bars = False

    if no_ssl_verify:
        c.session.downloads.verify_ssl = False

    ctx.obj["config"] = c


@rip.command()
@click.argument("urls", nargs=-1, required=True)
@click.pass_context
@coro
async def url(ctx, urls):
    """Download content from URLs."""
    _require_config(ctx)

    async with main_session(ctx) as main:
        await main.add_all(urls)
        await main.resolve()
        await main.rip()


@rip.command()
@click.argument(
    "path",
    required=True,
    type=click.Path(exists=True, readable=True, file_okay=True, dir_okay=False),
)
@click.pass_context
@coro
async def file(ctx, path):
    """Download content from URLs in a file.

    Example usage:

        streamrip file urls.txt
    """
    _require_config(ctx)
    async with main_session(ctx) as main:
        async with aiofiles.open(path, "r") as f:
            content = await f.read()
            try:
                items: Any = json.loads(content)
                loaded = True
            except json.JSONDecodeError:
                items = content.split()
                loaded = False
        if loaded:
            console.print(
                f"Detected json file. Loading [yellow]{len(items)}[/yellow] items"
            )
            await main.add_all_by_id(
                [(i["source"], i["media_type"], i["id"]) for i in items]
            )
        else:
            # dict, not set: keeps the file's order.
            unique = list(dict.fromkeys(items))
            if len(unique) < len(items):
                console.print(
                    f"Found [yellow]{len(items) - len(unique)}[/yellow] repeated URLs!"
                )
                items = unique
            console.print(
                f"Detected list of urls. Loading [yellow]{len(items)}[/yellow] items"
            )
            await main.add_all(items)

        await main.resolve()
        await main.rip()


@rip.group()
def config():
    """Manage configuration files."""


@config.command("open")
@click.option("-v", "--vim", help="Open in (Neo)Vim", is_flag=True)
@click.pass_context
def config_open(ctx, vim):
    """Open the config file in a text editor."""
    config_path = ctx.obj["config_path"]

    console.print(f"Opening file at [bold cyan]{config_path}")
    if vim:
        if shutil.which("nvim") is not None:
            subprocess.run(["nvim", config_path])
        elif shutil.which("vim") is not None:
            subprocess.run(["vim", config_path])
        else:
            logger.error("Could not find nvim or vim. Using default launcher.")
            click.launch(config_path)
    else:
        click.launch(config_path)


@config.command("reset")
@click.option("-y", "--yes", help="Don't ask for confirmation.", is_flag=True)
@click.pass_context
def config_reset(ctx, yes):
    """Reset the config file."""
    config_path = ctx.obj["config_path"]
    if not yes:
        if not Confirm.ask(
            f"Are you sure you want to reset the config file at {config_path}?",
        ):
            console.print("[green]Reset aborted")
            return

    set_user_defaults(config_path)
    console.print(f"Reset the config file at [bold cyan]{config_path}!")


@config.command("path")
@click.pass_context
def config_path(ctx):
    """Display the path of the config file."""
    config_path = ctx.obj["config_path"]
    console.print(f"Config path: [bold cyan]'{config_path}'")


@rip.group()
def database():
    """View and modify the downloads and failed downloads databases."""


@database.command("browse")
@click.argument(
    "table", type=click.Choice(["downloads", "failed"], case_sensitive=False)
)
@click.pass_context
def database_browse(ctx, table):
    """Browse the contents of a table.

    Available tables:

        * downloads

        * failed
    """
    from rich.table import Table

    cfg = _require_config(ctx)

    if table.lower() == "downloads":
        t = Table("Row", "Source", "ID", title="Downloads database")
        # A row from before ids carried their source has none to show.
        rows = [
            (source or "unknown", item_id)
            for source, item_id in (
                db.split_download_key(key)
                for (key,) in db.Downloads(cfg.session.database.downloads_path).all()
            )
        ]
    else:
        t = Table(
            "Row", "Source", "Media Type", "ID", title="Failed downloads database"
        )
        rows = db.Failed(cfg.session.database.failed_downloads_path).all()
    for i, row in enumerate(rows):
        t.add_row(f"{i:02}", *row)
    console.print(t)


@database.command("clear")
@click.argument(
    "table",
    type=click.Choice(["downloads", "failed", "all"], case_sensitive=False),
)
@click.option("-y", "--yes", help="Don't ask for confirmation.", is_flag=True)
@click.pass_context
def database_clear(ctx, table, yes):
    """Forget what was downloaded, so it is downloaded again.

    streamrip skips every track in the downloads database, even after its file
    was deleted. Clear the database to download those tracks again.

    Tables:

        * downloads: the tracks streamrip has downloaded

        * failed: the failures kept for `streamrip repair`

        * all: both
    """
    cfg = _require_config(ctx)

    tables = []
    if table in ("downloads", "all"):
        tables.append(
            ("downloaded track(s)", db.Downloads(cfg.session.database.downloads_path))
        )
    if table in ("failed", "all"):
        tables.append(
            (
                "failed download(s)",
                db.Failed(cfg.session.database.failed_downloads_path),
            )
        )

    counts = [len(t.all()) for _, t in tables]
    if sum(counts) == 0:
        console.print("[green]Nothing to clear.")
        return

    summary = " and ".join(
        f"[yellow]{n}[/yellow] {label}" for n, (label, _) in zip(counts, tables)
    )
    if not yes and not Confirm.ask(f"Forget {summary}? They will be downloaded again."):
        console.print("[green]Clear aborted")
        return

    for _, t in tables:
        t.clear()
    console.print(f"[green]Cleared {summary}.")


async def _albums_for(main, failed_items):
    """Map failed tracks onto the albums that contain them.

    Returns (album targets, items to retry as they are). Anything whose album
    cannot be determined is passed through untouched rather than dropped.
    """
    targets: list[tuple[str, str, str]] = []
    unresolved: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for source, media_type, item_id in failed_items:
        if media_type != "track":
            targets.append((source, media_type, item_id))
            continue
        try:
            client = await main.get_logged_in_client(source)
            resp = await client.get_metadata(item_id, "track")
            album_id = str((resp.get("album") or {}).get("id") or "")
        except Exception as e:
            logger.debug("Could not find the album for %s: %s", item_id, e)
            album_id = ""

        if not album_id:
            unresolved.append((source, media_type, item_id))
            continue
        if (source, album_id) not in seen:
            seen.add((source, album_id))
            targets.append((source, "album", album_id))

    return targets, unresolved


@rip.command()
@click.option("-y", "--yes", help="Don't ask for confirmation.", is_flag=True)
@click.option(
    "--flat",
    help="Put repaired tracks straight in the download folder instead of "
    "their album folder.",
    is_flag=True,
)
@click.pass_context
@coro
async def repair(ctx, yes, flat):
    """Retry downloads that previously failed.

    Reads the failed downloads database, retries each item, and clears it
    from the failed database on success. Items that fail again stay logged
    so they can be retried later.

    Failed tracks are retried individually, but are placed in their album's
    folder so they rejoin the album they were originally missing from. Pass
    --flat to put them in the download folder instead.
    """
    with _require_config(ctx) as cfg:
        cfg: Config
        # A repaired track is nearly always a track missing from an album that
        # was otherwise downloaded, so it needs to land in that album's folder
        # rather than loose in the download root. This only touches the
        # in-memory session copy, so config.toml is left alone.
        if not flat:
            cfg.session.filepaths.add_singles_to_folder = True
        database = db.Database(
            db.Downloads(cfg.session.database.downloads_path),
            db.Failed(cfg.session.database.failed_downloads_path),
        )
        failed_items = database.failed.all()

        if not failed_items:
            console.print("[green]No failed downloads to repair!")
            return

        console.print(f"Found [yellow]{len(failed_items)}[/yellow] failed download(s).")
        if not yes and not Confirm.ask("Retry them now?"):
            console.print("[green]Repair aborted")
            return

        # A failed item should never also be logged as downloaded, but older
        # versions of streamrip could mark one downloaded even after it
        # failed. Clear that stale state so the retry below isn't skipped.
        for source, _media_type, item_id in failed_items:
            database.forget_downloaded(source, item_id)

        async with Main(cfg) as main:
            # Retry through the album rather than track by track. Resolving a
            # single track builds its album metadata from the track response,
            # which on Tidal carries only an id, title and cover -- no track
            # count, and the track's artists in place of the album artist. The
            # folder that produces differs from the album's own in both, so
            # repaired tracks land in a separate folder instead of rejoining
            # the album.
            #
            # Going through the album gets the real metadata, the right
            # folder, disc subfolders and cover art, and costs nothing extra:
            # tracks already in the downloads db are skipped, so only what is
            # missing gets fetched.
            targets, unresolved = await _albums_for(main, failed_items)
            if unresolved:
                console.print(
                    f"[yellow]{len(unresolved)} item(s) had no album to retry "
                    "through; fetching them individually."
                )
            await main.add_all_by_id(targets + unresolved)
            await main.resolve()
            await main.rip()

        # Nothing in the download pipeline removes rows from the failed db, so
        # success can't be detected by diffing it. But set_downloaded() is
        # only reached via postprocess(), which a failed download never gets
        # to, so an item in the downloads db now is one that just succeeded.
        repaired = [
            (source, media_type, item_id)
            for source, media_type, item_id in failed_items
            if database.downloaded(source, item_id)
        ]
        for source, media_type, item_id in repaired:
            database.forget_failed(source, media_type, item_id)

        console.print(
            f"[green]Repaired {len(repaired)}/{len(failed_items)} item(s).[/green]"
        )
        if len(repaired) < len(failed_items):
            console.print(
                f"[yellow]{len(failed_items) - len(repaired)} item(s) failed again "
                "and are still logged. Run [bold]streamrip repair[/bold] to try again."
            )


@rip.command()
@click.option(
    "-f",
    "--first",
    help="Automatically download the first search result without showing the menu.",
    is_flag=True,
)
@click.option(
    "-o",
    "--output-file",
    help="Write search results to a file instead of showing interactive menu.",
    type=click.Path(writable=True),
)
@click.option(
    "-n",
    "--num-results",
    help="Maximum number of search results to show "
    "(default: [cli] max_search_results in the config)",
    type=click.IntRange(min=1),
)
@click.argument("source", required=True)
@click.argument("media-type", required=True)
@click.argument("query", required=True)
@click.pass_context
@coro
async def search(ctx, first, output_file, num_results, source, media_type, query):
    """Search for content using a specific source.

    Example:

        streamrip search qobuz album 'rumours'
    """
    cfg = _require_config(ctx)
    if first and output_file:
        console.print("Cannot choose --first and --output-file!")
        return
    limit = num_results or cfg.session.cli.max_search_results
    async with main_session(ctx) as main:
        if first:
            await main.search_take_first(source, media_type, query)
        elif output_file:
            await main.search_output_file(source, media_type, query, output_file, limit)
        else:
            await main.search_interactive(source, media_type, query, limit)
        await main.resolve()
        await main.rip()


@rip.command()
@click.option("-s", "--source", help="The source to search tracks on.")
@click.option(
    "-fs",
    "--fallback-source",
    help="The source to search tracks on if no results were found with the main source.",
)
@click.argument("url", required=True)
@click.pass_context
@coro
async def lastfm(ctx, source, fallback_source, url):
    """Download tracks from a last.fm playlist."""
    config = _require_config(ctx)
    if source is not None:
        config.session.lastfm.source = source
    if fallback_source is not None:
        config.session.lastfm.fallback_source = fallback_source
    async with main_session(ctx) as main:
        await main.resolve_lastfm(url)
        await main.rip()


# Where a CSV list can be searched, and what that gives: shown when asked.
CSV_SOURCES = {
    "qobuz": "lossless, up to 24-bit",
    "tidal": "lossless, hi-res where there is some",
    "deezer": "FLAC or MP3, as your subscription allows",
    "spotify": "lossy: the audio comes from YouTube Music",
    "soundcloud": "MP3, and only uploads by the artists themselves",
}


def _ask_source(count: int) -> str:
    """Ask where to search and download the tracks of a CSV list from."""
    if not sys.stdin.isatty():
        # Never block on a prompt nobody is there to answer (cron, scripts).
        raise click.UsageError(
            f"Say where to search the tracks with --source ({', '.join(CSV_SOURCES)})."
        )
    console.print(
        f"\nFound [bold]{count}[/bold] track(s). "
        "Where should I search and download them from?"
    )
    for name, what in CSV_SOURCES.items():
        console.print(f"  [bold]{name:<11}[/bold] {what}")
    return Prompt.ask("Source", choices=list(CSV_SOURCES))


@rip.command("csv")
@click.option(
    "-s",
    "--source",
    type=click.Choice(list(CSV_SOURCES), case_sensitive=False),
    help="The source to search the tracks on. Asked when left out.",
)
@click.option(
    "-fs",
    "--fallback-source",
    type=click.Choice(list(CSV_SOURCES), case_sensitive=False),
    help="The source to search for a track the main source does not have.",
)
@click.argument(
    "path",
    required=True,
    type=click.Path(exists=True, readable=True, file_okay=True, dir_okay=False),
)
@click.pass_context
@coro
async def csv_list(ctx, source, fallback_source, path):
    """Download the tracks of a CSV list, found by searching a source.

    One track per row: its title and artist, and its album and length if you
    have them. The columns of Music-Sync (https://github.com/Stensel8/Music-Sync)
    and of other exporters are understood; a file without a header row is read
    as "artist,title". The tracks go in a folder named after the file.

    Example usage:

        streamrip csv playlist.csv

        streamrip csv playlist.csv --source tidal --fallback-source qobuz
    """
    _require_config(ctx)
    try:
        tracks = read_tracks(path)
    except (OSError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    if not tracks:
        raise click.ClickException(
            f"No tracks found in {path}: it needs a title for every row."
        )
    if source is None:
        source = _ask_source(len(tracks))
    async with main_session(ctx) as main:
        name = os.path.splitext(os.path.basename(path))[0]
        await main.resolve_csv(name, tracks, source.lower(), fallback_source)
        await main.rip()


@rip.command()
@click.argument("source")
@click.argument("media-type")
@click.argument("id")
@click.pass_context
@coro
async def id(ctx, source, media_type, id):
    """Download an item by ID."""
    _require_config(ctx)
    async with main_session(ctx) as main:
        await main.add_by_id(source, media_type, id)
        await main.resolve()
        await main.rip()


def _version_tuple(version: str) -> tuple[int, ...]:
    """Numeric version for comparisons: "2.10.0" > "2.9.1", "v2.3" == "2.3"."""
    return tuple(int(n) for n in re.findall(r"\d+", version.split("+")[0])[:3])


def is_newer_version(latest: str | None, current: str = __version__) -> bool:
    if not latest:
        return False
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except ValueError:
        return False


async def latest_streamrip_version(
    verify_ssl: bool = True,
) -> tuple[str, str | None, bool]:
    """Get the latest version of this fork and its release notes from GitHub.

    Uses the latest GitHub release, or the version in pyproject.toml on the
    default branch when the repository has no releases. Network problems are
    never fatal: the check is simply skipped (upstream PR #995).

    Args:
        verify_ssl: Whether to verify SSL certificates

    Returns:
        A tuple of (version, release_notes, is_release)
    """
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with new_session(verify_ssl=verify_ssl, timeout=timeout) as s:
            async with s.get(
                f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            ) as resp:
                if resp.status == 200:
                    release = await resp.json(content_type=None)
                    tag = str(release.get("tag_name") or "").lstrip("vV")
                    if tag:
                        return tag, release.get("body"), True

            async with s.get(
                f"https://raw.githubusercontent.com/{REPOSITORY}/HEAD/pyproject.toml"
            ) as resp:
                if resp.status == 200:
                    match = re.search(
                        r'^version\s*=\s*"([^"]+)"', await resp.text(), re.MULTILINE
                    )
                    if match:
                        return match.group(1), None, False
    except Exception as e:
        logger.debug("Could not check for updates: %s", e)
    return __version__, None, False


if __name__ == "__main__":
    rip()
