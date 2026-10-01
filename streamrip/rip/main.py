import asyncio
import json
import logging
import platform
import sys

import aiofiles

from .. import db
from ..client import Client, DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from ..config import Config
from ..console import console
from ..exceptions import (
    APIError,
    AuthenticationError,
    FFmpegNotFoundError,
    MissingCredentialsError,
)
from ..media import (
    Artist,
    Label,
    Media,
    Pending,
    PendingLastfmPlaylist,
    pending_item,
    remove_artwork_tempdirs,
)
from ..media.media import resolve_or_none
from ..metadata import SearchResults
from ..progress import clear_progress, clear_screen
from ..utils.ffmpeg_utils import ffmpeg_missing_message, find_ffmpeg
from .interactive import Confirm
from .parse_url import parse_url
from .prompter import get_prompter

logger = logging.getLogger("streamrip")

# Windows used to be forced onto the SelectorEventLoop because aiodns needed it,
# but that loop cannot run subprocesses, so every ffmpeg conversion failed there.
# aiodns is no longer used (connectors use aiohttp's ThreadedResolver), so the
# default ProactorEventLoop is kept.


class Main:
    """Provides all of the functionality called into by the CLI.

    * Logs in to Clients and prompts for credentials
    * Handles output logging
    * Handles downloading Media
    * Handles interactive search

    User input (urls) -> Main --> Download files & Output messages to terminal
    """

    def __init__(self, config: Config):
        # Data pipeline:
        # input URL -> (URL) -> (Pending) -> (Media) -> (Downloadable) -> audio file
        self.pending: list[Pending] = []
        self.media: list[Media] = []
        self.config = config
        self.clients: dict[str, Client] = {
            "qobuz": QobuzClient(config),
            "tidal": TidalClient(config),
            "deezer": DeezerClient(config),
            "soundcloud": SoundcloudClient(config),
        }

        self.database: db.Database

        c = self.config.session.database
        if c.downloads_enabled:
            downloads_db = db.Downloads(c.downloads_path)
        else:
            downloads_db = db.Dummy()

        if c.failed_downloads_enabled:
            failed_downloads_db = db.Failed(c.failed_downloads_path)
        else:
            failed_downloads_db = db.Dummy()

        self.database = db.Database(downloads_db, failed_downloads_db)

    async def add(self, url: str):
        """Add url as a pending item.

        Do not `asyncio.gather` calls to this! Use `add_all` for concurrency.
        """
        parsed = parse_url(url)
        if parsed is None:
            raise Exception(f"Unable to parse url {url}")

        client = await self.get_logged_in_client(parsed.source)
        self.pending.append(
            await parsed.into_pending(client, self.config, self.database),
        )
        logger.debug("Added url=%s", url)

    async def add_by_id(self, source: str, media_type: str, id: str):
        await self.add_all_by_id([(source, media_type, id)])

    async def add_all_by_id(self, info: list[tuple[str, str, str]]):
        sources = set(s for s, _, _ in info)
        clients = {s: await self.get_logged_in_client(s) for s in sources}
        for source, media_type, id in info:
            self.pending.append(
                pending_item(
                    media_type, id, clients[source], self.config, self.database
                )
            )

    async def add_all(self, urls: list[str]):
        """Add multiple urls concurrently as pending items."""
        parsed = [parse_url(url) for url in urls]
        url_client_pairs = []
        for i, p in enumerate(parsed):
            if p is None:
                console.print(
                    f"[red]Found invalid url [cyan]{urls[i]}[/cyan], skipping.",
                )
                continue
            url_client_pairs.append((p, await self.get_logged_in_client(p.source)))

        pendings = await asyncio.gather(
            *[
                url.into_pending(client, self.config, self.database)
                for url, client in url_client_pairs
            ],
        )
        self.pending.extend(pendings)

    async def get_logged_in_client(self, source: str):
        """Return a functioning client instance for `source`."""
        client = self.clients.get(source)
        if client is None:
            raise Exception(
                f"No client named {source} available. Only have {self.clients.keys()}",
            )
        if not client.logged_in:
            prompter = get_prompter(client, self.config)
            if not prompter.has_creds():
                # Get credentials from user and log into client
                await prompter.prompt_and_login()
                prompter.save()
            else:
                try:
                    with console.status(f"[cyan]Logging into {source}", spinner="dots"):
                        # Log into client using credentials from config
                        await client.login()
                except (AuthenticationError, MissingCredentialsError) as e:
                    # has_creds() only checks that something is *stored*, not
                    # that it still works. Saved tokens expire, so the usual
                    # way to discover a lapsed login is a failure here -- which
                    # used to be a bare traceback, even though the prompter
                    # that fixes it is already built and sitting right there.
                    await self._reauthenticate(source, prompter, e)

        assert client.logged_in
        return client

    async def _reauthenticate(self, source: str, prompter, cause: Exception):
        """Offer a fresh login after stored credentials stop working."""
        console.print(f"[yellow]{source.title()} login failed: {cause}")

        # Never block on a prompt nobody is there to answer: streamrip is run from
        # cron and from scripts, where a hidden y/n means hanging forever
        # rather than failing.
        if not sys.stdin.isatty():
            raise AuthenticationError(
                f"{source} needs authorising again. Re-run this from an "
                f"interactive terminal and you will be prompted to log in."
            ) from cause

        if not Confirm.ask(f"Log into {source} again now?"):
            raise cause

        await prompter.prompt_and_login()
        prompter.save()

    async def resolve(self):
        """Resolve all currently pending items."""
        with console.status("Resolving URLs...", spinner="dots"):
            resolved = await asyncio.gather(*map(resolve_or_none, self.pending))
            new_media: list[Media] = [m for m in resolved if m is not None]

        self.media.extend(new_media)
        self.pending.clear()

    async def rip(self):
        """Download all resolved items, one at a time, top to bottom.

        An Artist or Label is its entire discography -- running several of
        those at once means every one of them has an album's worth of tracks
        in flight together, which both interleaves the progress display
        beyond following and multiplies how hard the streaming service's
        rate limit gets hit at once. Finishing one item completely before
        starting the next keeps both predictable, in exchange for not
        overlapping items that could otherwise run independently.
        """
        failed_items = 0
        for i, item in enumerate(self.media):
            # An artist or label logs a whole discography's worth of lines;
            # clear the previous item's off screen so only the one now
            # running is shown.
            if i > 0 and isinstance(item, Artist | Label):
                clear_screen(self.config.session.cli.progress_bars)
            try:
                await item.rip()
            except Exception as e:
                logger.error(f"Error processing media item: {type(e).__name__}: {e}")
                failed_items += 1

        # In tracks, not items: an item can be a whole discography, so "1
        # item downloaded" said nothing about what actually happened.
        d = self.database
        parts = [f"{d.downloaded_now} track(s) downloaded"]
        if d.skipped_now:
            parts.append(f"{d.skipped_now} already downloaded")
        if d.failed_now:
            parts.append(f"{d.failed_now} failed")
        summary = "Download completed: " + ", ".join(parts)
        if failed_items:
            summary += (
                f"; {failed_items} of {len(self.media)} item(s) could not be processed"
            )
        logger.info(summary)
        if d.failed_now and not isinstance(d.failed, db.Dummy):
            logger.info("Run `streamrip repair` to retry the failed tracks.")

    async def _search(
        self, source: str, media_type: str, query: str, limit: int
    ) -> SearchResults | None:
        """Search results, or None (with a message) if there are none."""
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            try:
                pages = await client.search(media_type, query, limit=limit)
            except APIError as e:
                console.print(f"[red]Search failed: {e}")
                return None
        # A page can come back with no items in it, so count results, not pages.
        search_results = SearchResults.from_pages(source, media_type, pages)
        if not search_results.results:
            console.print(f"[red]No search results found for query {query}")
            return None
        return search_results

    async def search_interactive(
        self, source: str, media_type: str, query: str, limit: int = 100
    ):
        """Search, then let the user pick results from an interactive menu."""
        search_results = await self._search(source, media_type, query, limit)
        if search_results is None:
            return

        if platform.system() == "Windows":  # simple term menu not supported for windows
            from pick import pick

            choices = pick(
                search_results.results,
                title=(
                    f"{source.capitalize()} {media_type} search.\n"
                    "Press SPACE to select, RETURN to download, CTRL-C to exit."
                ),
                multiselect=True,
                min_selection_count=1,
            )
            assert isinstance(choices, list)

            await self.add_all_by_id(
                [(source, media_type, item.id) for item, _ in choices],
            )

        else:
            from simple_term_menu import TerminalMenu

            menu = TerminalMenu(
                search_results.summaries(),
                preview_command=search_results.preview,
                preview_size=0.5,
                title=(
                    f"Results for {media_type} '{query}' from {source.capitalize()}\n"
                    "SPACE - select, ENTER - download, ESC - exit"
                ),
                cycle_cursor=True,
                clear_screen=True,
                multi_select=True,
            )
            chosen_ind = menu.show()
            if chosen_ind is None:
                console.print("[yellow]No items chosen. Exiting.")
            else:
                choices = search_results.get_choices(chosen_ind)
                await self.add_all_by_id(
                    [(source, item.media_type(), item.id) for item in choices],
                )

    async def search_take_first(self, source: str, media_type: str, query: str):
        """Search and queue only the first result, with no user interaction."""
        search_results = await self._search(source, media_type, query, 1)
        if search_results is not None:
            first = search_results.results[0]
            await self.add_by_id(source, first.media_type(), first.id)

    async def search_output_file(
        self, source: str, media_type: str, query: str, filepath: str, limit: int
    ):
        """Search and write the results to filepath as JSON, without downloading."""
        search_results = await self._search(source, media_type, query, limit)
        if search_results is None:
            return

        file_contents = json.dumps(search_results.as_list(source), indent=4)
        async with aiofiles.open(filepath, "w") as f:
            await f.write(file_contents)

        console.print(
            f"Wrote [purple]{len(search_results.results)}[/purple] results to [cyan]{filepath} as JSON!"
        )

    async def resolve_lastfm(self, playlist_url: str):
        """Resolve a last.fm playlist."""
        c = self.config.session.lastfm
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingLastfmPlaylist(
            playlist_url,
            client,
            fallback_client,
            self.config,
            self.database,
        )
        playlist = await pending_playlist.resolve()

        if playlist is not None:
            self.media.append(playlist)

    async def __aenter__(self):
        """Return this session, raising FFmpegNotFoundError if ffmpeg is missing."""
        # ffmpeg is required whatever is downloaded (Tidal hi-res, SoundCloud,
        # conversion), so check before logging in to anything.
        if find_ffmpeg() is None:
            raise FFmpegNotFoundError(ffmpeg_missing_message())
        return self

    async def __aexit__(self, *_):
        """Close every client session and clean up progress bars and artwork."""
        # Ensure all client sessions are closed
        for client in self.clients.values():
            if isinstance(client, TidalClient):
                await client.close()  # both of its logins have a session
            elif hasattr(client, "session"):
                await client.session.close()

        # close global progress bar manager
        clear_progress()
        # We remove artwork tempdirs here because multiple singles
        # may be able to share downloaded artwork in the same `streamrip` session
        # We don't know that a cover will not be used again until end of execution
        remove_artwork_tempdirs()
