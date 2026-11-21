"""`streamrip playlist`: move liked songs and playlists between Spotify, Tidal and
CSV files, or download them from a lossless source.

What Music-Sync's `music-sync` command did, as part of streamrip. The work
itself is in streamrip/sync; this is the command line around it.
"""

import logging
import secrets
import shutil
import sys
import time
from functools import wraps
from itertools import count
from pathlib import Path
lazy import asyncio
lazy from collections.abc import Callable

import click

lazy from ..client.spotify import SpotifyClient
lazy from ..config import Config
lazy from ..console import console, err_console, plural
lazy from ..exceptions import AuthenticationError
lazy from ..sync.auth import pkce_pair
lazy from ..sync.csvio import read_tracks, slug, write_tracks
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
lazy from ..sync.errors import ProviderError, SyncError
lazy from ..sync.models import Track
lazy from ..sync.providers.base import Provider
lazy from ..sync.services import DEVELOPER_DASHBOARDS, LABELS, SERVICES, Services
lazy from .cli import _ask_source, main_session
lazy from .interactive import Prompt
lazy from .prompter import SpotifyPrompter, has_screen, launch
lazy from .spotify_login import capture_code, code_from_redirect_url

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


def free_name(name: str, taken: set[str]) -> str:
    """`name`, or "name (2)" and so on when an earlier file took it. Two
    playlists can share a name, and `slug` makes more names alike; "Mix" and
    "mix" are one file on Windows and macOS."""
    free = next(
        n
        for i in count(1)
        if (n := name if i == 1 else f"{name} ({i})").casefold() not in taken
    )
    taken.add(free.casefold())
    return free


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
        streamrip playlist login spotify
        streamrip playlist transfer spotify tidal --liked
        streamrip playlist export spotify --all -o backup/
        streamrip playlist import tidal liked.csv --playlist "From CSV"
        streamrip playlist download spotify --playlist "Road trip" --source qobuz
    """


# --- logging in -----------------------------------------------------------------


def _browser_code(url: str, redirect_uri: str, state: str, label: str) -> str:
    """Send the user to `url` to log in, and take the code back: caught on
    this computer, or pasted from a browser elsewhere."""
    console.print(
        f"\nHow do you want to log in to {label}?\n"
        "  1. In the browser on this computer: streamrip catches the login itself\n"
        "  2. In a browser on another device (streamrip runs on a server, say):\n"
        "     open the link there and paste the address you end up on\n"
    )
    choice = Prompt.ask(
        "Choose", choices=["1", "2"], default="1" if has_screen() else "2"
    )
    if choice == "1":
        console.print(
            f"\nOpening your browser. If nothing opens, go to:\n{url}\n"
            f"{label} must send you back to [bold]{redirect_uri}[/bold]; add that "
            "to your app as a redirect URI if it says it is not allowed."
        )
        launch(url)
        return asyncio.run(capture_code(redirect_uri, state, label))
    console.print(f"\nOpen this link and log in:\n{url}\n")
    console.print(
        "The page you end up on will not load: that is as it should be. "
        "Copy its whole address from the address bar."
    )
    return code_from_redirect_url(Prompt.ask("Paste the address"), state, label)


async def _spotify_login(config: Config):
    """The login streamrip's Spotify downloads use, asked for again: the one
    login is for both."""
    client = SpotifyClient(config)
    try:
        prompter = SpotifyPrompter(config, client)
        await prompter.prompt_and_login()
        prompter.save()
    finally:
        await client.close()


def _tidal_app(services: Services):
    """Ask for the Tidal developer app, unless the config has one."""
    tidal = services.config.session.tidal
    if tidal.api_client_id and tidal.api_client_secret:
        return
    console.print(
        "\n[bold]Tidal's official API needs a developer app of your own.[/bold] "
        "It is free and takes a minute. (Downloading from Tidal does not need it; "
        "only reading and changing your playlists and favourites does.)\n\n"
        f"  1. Go to [blue underline]{DEVELOPER_DASHBOARDS['tidal']}[/blue underline] "
        "and choose [italic]Create New App[/italic].\n"
        f"  2. Redirect URI: [bold]{tidal.api_redirect_uri}[/bold]\n"
        "  3. Scopes: collection.read, collection.write, playlists.read, "
        "playlists.write and user.read\n"
        "  4. Copy its Client ID and Client Secret.\n"
    )
    launch(DEVELOPER_DASHBOARDS["tidal"])
    client_id = ""
    while not client_id:
        client_id = Prompt.ask("Enter the Client ID of your Tidal app").strip()
    secret = ""
    while not secret:
        secret = Prompt.ask(
            "Enter its Client Secret (invisible)", password=True
        ).strip()
    for data in (services.config.session, services.config.file):
        data.tidal.api_client_id, data.tidal.api_client_secret = client_id, secret
    services.config.file.set_modified()
    services.config.save_file()


def login_tidal_api(services: Services):
    """Log in to Tidal's official API in the browser, and keep the login."""
    _tidal_app(services)
    oauth = services.oauth("tidal")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    code = _browser_code(
        oauth.authorize_url(state, challenge), oauth.redirect_uri, state, "Tidal"
    )
    services.store.save("tidal", oauth.exchange(code, verifier))


@playlist.command()
@click.argument("service", type=SERVICE)
@click.pass_context
@_explained
def login(ctx, service):
    """Log in to a service in your browser.

    Spotify: the login streamrip downloads with, now also allowed to change
    your library. Tidal: a login to its official API with a developer app of
    your own, next to the login streamrip downloads with.
    """
    services = _services(ctx)
    if service == "spotify":
        try:
            asyncio.run(_spotify_login(services.config))
        except AuthenticationError as e:
            raise click.ClickException(str(e)) from e
        services.config.save_file()
    else:
        login_tidal_api(services)
    console.print(f"Logged in to {LABELS[service]}.")


@playlist.command()
@click.argument("service", type=SERVICE)
@click.pass_context
@_explained
def logout(ctx, service):
    """Forget the saved login of a service.

    For Spotify that is also the login streamrip downloads with; for Tidal only
    the official API's.
    """
    removed = _services(ctx).store.delete(service)
    label = LABELS[service]
    console.print(
        f"Logged out of {label}." if removed else f"Not logged in to {label}."
    )


@playlist.command()
@click.pass_context
@_explained
def status(ctx):
    """Show which apps are set up and logged in."""
    services = _services(ctx)
    for service in SERVICES:
        token = services.store.load(service)
        if token is None:
            login = "not logged in"
        elif token.expired() and not token.refresh_token:
            login = "login expired"
        else:
            login = "logged in"
        app = "set up" if services.is_configured(service) else "MISSING"
        console.print(f"{LABELS[service]:8} app: {app:8} {login}", highlight=False)
    console.print(f"Read from {services.config.path}", highlight=False)


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
    help="The CSV file (a folder with --all).",
)
@click.option("-q", "--quiet", is_flag=True, help="No progress output.")
@click.pass_context
@_explained
def export(ctx, service, liked, playlist, everything, output, quiet):
    """Save liked songs or playlists as CSV files.

    The files read back in with `streamrip playlist import` and `streamrip csv`,
    and in Exportify's and most other tools' format.
    """
    _selection(liked, playlist, everything)
    provider = _services(ctx).provider(service)
    selected = _select(provider, liked, playlist, _progress(quiet))
    if everything:  # one CSV per playlist, in a folder
        folder = output or Path("streamrip-export") / service
        taken: set[str] = set()
        for name, tracks in selected:
            path = folder / f"{free_name(slug(name), taken)}.csv"
            console.print(
                f"{write_tracks(path, tracks):>5} tracks -> {path}", highlight=False
            )
    else:
        ((name, tracks),) = selected
        path = output or Path(f"{slug(name)}.csv")
        console.print(f"{write_tracks(path, tracks)} tracks -> {path}", highlight=False)


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
    provider, progress = _services(ctx).provider(service), _progress(quiet)
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
        )
    console.print(result.summary(), markup=False, highlight=False)
    _report_unmatched(result.misses, unmatched)


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
    progress = _progress(quiet)
    misses: list[Miss] = []
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
        )
        console.print(result.summary(), markup=False, highlight=False)
        misses.extend(result.misses)
    _report_unmatched(misses, unmatched)


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
    type=click.Choice(["qobuz", "tidal", "deezer", "soundcloud", "spotify"]),
    help="Where to look for a track the first source does not have.",
)
@click.option("-q", "--quiet", is_flag=True, help="No progress output while reading.")
@click.pass_context
def download(ctx, service, liked, playlist, everything, source, fallback_source, quiet):
    """Download liked songs or playlists from another source.

    Reads the list from Spotify or Tidal and finds each track on the source you
    pick, the way `streamrip csv` does: a Spotify playlist in lossless from
    Qobuz, Tidal or Deezer, say, where a Spotify link gets YouTube Music's AAC.
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
            await main.rip()

    asyncio.run(rip())


# --- checking the setup -----------------------------------------------------------


def _probes(provider: Provider) -> list[tuple[str, Callable[[], str]]]:
    """The live checks `doctor` runs against one service, in order."""
    first_liked: list[Track] = []  # filled by the first probe, used by the ISRC one

    def liked() -> str:
        if track := next(iter(provider.liked_tracks()), None):
            first_liked.append(track)
            return f"first liked track: {track}"
        return "library is empty"

    def playlists() -> str:
        return plural(len(provider.playlists()), "playlist")

    def search() -> str:
        return plural(len(provider.search("Daft Punk Get Lucky")), "result")

    def isrc() -> str:
        code = first_liked[0].isrc if first_liked else None
        if not code:
            return "skipped (no liked track with an ISRC to test with)"
        if not provider.lookup_isrcs([code]).get(code):
            raise ProviderError(f"lookup of {code} found nothing")
        return f"{code} found"

    return [
        ("read liked songs", liked),
        ("list playlists", playlists),
        ("search the catalogue", search),
        ("look up by ISRC", isrc),
    ]


@playlist.command()
@click.argument("service", type=SERVICE, required=False)
@click.pass_context
def doctor(ctx, service):
    """Try your setup against the real APIs.

    Add its output to an issue when something fails.
    """
    services = _services(ctx)
    healthy = True
    for name in [service] if service else SERVICES:
        console.print(LABELS[name])
        try:
            probes = _probes(services.provider(name))  # fails here without an app
        except SyncError as e:
            console.print(f"  FAIL  setup: {e}", markup=False, highlight=False)
            healthy = False
            continue
        console.print("  ok    setup")
        for label, probe in probes:
            try:
                console.print(
                    f"  ok    {label}: {probe()}", markup=False, highlight=False
                )
            except SyncError as e:
                console.print(f"  FAIL  {label}: {e}", markup=False, highlight=False)
                healthy = False
    if not healthy:
        ctx.exit(1)
