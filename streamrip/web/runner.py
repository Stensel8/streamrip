"""streamrip's downloads, searches and logins, run for the web interface.

Downloads are async; the web server is not. They run on an event loop of their
own, in a thread, one download at a time (a second waits for the first, as two
`streamrip` commands would fight over the same accounts' rate limits).
"""

import asyncio
import logging
import threading
from dataclasses import dataclass
lazy from collections.abc import Awaitable, Callable, Coroutine
lazy from typing import Any

from .. import progress
from ..client import DeezerClient, QobuzClient, SpotifyClient, TidalClient
from ..console import plural
from ..exceptions import AuthenticationError, MissingCredentialsError
from ..metadata import SearchResults
from ..rip.main import Main
from ..rip.prompter import DeezerPrompter, QobuzPrompter, TidalPrompter
from .jobs import JobLog, JobObserver
lazy from ..config import Config
lazy from .jobs import Job

logger = logging.getLogger("streamrip")

SOURCES = ("qobuz", "tidal", "deezer", "soundcloud", "spotify")
MEDIA_TYPES = ("track", "album", "artist", "playlist")
SEARCH_RESULTS = 30  # what the page lists of a search


@dataclass(slots=True)
class Result:
    """A search result as the page shows it."""

    source: str
    media_type: str
    id: str
    name: str
    artist: str | None
    year: str | None
    explicit: bool
    details: list[tuple[str, str]]
    image: str | None


class Runner:
    """The event loop downloads run on, and whose turn it is."""

    def __init__(self, config: Config):
        self.config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Lock()
        self._turn = threading.Lock()  # one download at a time

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._started:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever, name="streamrip-downloads", daemon=True
                ).start()
                self._loop = loop
            return self._loop

    def call[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run `coro` on the downloads' loop and wait for what it returns."""
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result()

    # --- downloading -------------------------------------------------------

    def download(self, job: Job, add: Callable[[Main], Awaitable[None]]) -> str:
        """Log in, `add` what to download to a run, and download it, showing
        the progress on `job`. Returns the summary."""
        if not self._turn.acquire(blocking=False):
            job.text = "Waiting for the download before this one to finish"
            self._turn.acquire()
        handler = JobLog(job)
        logger.addHandler(handler)
        progress.observe(JobObserver(job))
        try:
            return self.call(self._run(job, add))
        finally:
            progress.observe(None)
            logger.removeHandler(handler)
            self._turn.release()

    async def _run(self, job: Job, add: Callable[[Main], Awaitable[None]]) -> str:
        # No bars in the terminal the server runs in: the page shows them.
        self.config.session.cli.progress_bars = False
        try:
            async with Main(self.config, interactive=False) as main:
                job.enter("find", "Logging in and looking everything up")
                await add(main)
                await main.resolve()
                if not main.media:
                    raise MissingCredentialsError(
                        "Nothing to download: see the log below for why."
                    )
                job.enter("download", "Downloading")
                await main.rip()
                d = main.database
        finally:
            # A login refreshed on the way is kept, as the command line does.
            self.config.save_file()
        parts = [f"{plural(d.downloaded_now, 'track')} downloaded"]
        if d.skipped_now:
            parts.append(f"{d.skipped_now} already downloaded")
        if d.failed_now:
            parts.append(f"{d.failed_now} failed (streamrip repair retries them)")
        return ", ".join(parts) + f", in {self.config.session.downloads.folder}"

    # --- searching ---------------------------------------------------------

    def search(self, source: str, media_type: str, query: str) -> list[Result]:
        return self.call(self._search(source, media_type, query))

    async def _search(self, source: str, media_type: str, query: str) -> list[Result]:
        main = Main(self.config, interactive=False)
        try:
            client = await main.get_logged_in_client(source)
            pages = await client.search(media_type, query, limit=SEARCH_RESULTS)
        finally:
            for client in main.clients.values():
                await client.close()
            self.config.save_file()
        found = SearchResults.from_pages(source, media_type, pages).results
        return [
            Result(
                source,
                r.media_type,
                str(r.id),
                r.name,
                r.artist,
                r.year,
                r.explicit,
                r.details,
                r.image_url,
            )
            for r in found[:SEARCH_RESULTS]
        ]

    # --- logging in to the download sources --------------------------------

    def logged_in(self) -> dict[str, bool]:
        """Whether each download source has a login saved (not whether it
        still works: that shows when it is used)."""
        c = self.config.session
        return {
            "qobuz": bool(c.qobuz.user_id and c.qobuz.auth_token),
            "tidal": bool(c.tidal.access_token),
            "deezer": bool(c.deezer.arl),
            "soundcloud": True,  # needs no login
            "spotify": bool(c.spotify.client_id and c.spotify.refresh_token),
        }

    def login_qobuz(self, user_id: str, token: str):
        """Log in to Qobuz with a user id and token, and keep them if they work."""
        self.call(self._login(QobuzClient, QobuzPrompter, qobuz=(user_id, token)))

    def login_deezer(self, arl: str):
        """Log in to Deezer with an ARL cookie, and keep it if it works."""
        self.call(self._login(DeezerClient, DeezerPrompter, deezer=arl))

    async def _login(self, client_type, prompter_type, **given):
        c = self.config.session
        before = (c.qobuz.user_id, c.qobuz.auth_token, c.deezer.arl)
        client = client_type(self.config)
        try:
            if "qobuz" in given:
                c.qobuz.user_id, c.qobuz.auth_token = given["qobuz"]
            else:
                c.deezer.arl = given["deezer"]
            await client.login()
        except Exception:
            c.qobuz.user_id, c.qobuz.auth_token, c.deezer.arl = before
            raise
        finally:
            await client.close()
        prompter_type(self.config, client).save()
        self.config.save_file()

    def login_tidal(self, job: Job):
        """Tidal's device login, for downloading: a link to open, once per
        client (twice with the hi-res one)."""

        def show(link: str, minutes: int, which: str):
            job.link = link
            job.text = (
                f"Open the link and log in to Tidal within {minutes} minutes "
                f"(the {which} login)."
            )

        async def log_in():
            client = TidalClient(self.config)
            prompter = TidalPrompter(self.config, client)
            prompter.show_link = show
            try:
                for lane in client.lanes():
                    await prompter._device_login(lane)
                client.logged_in = True
            finally:
                await client.close()
            prompter.save()
            self.config.save_file()

        job.enter("login", "Asking Tidal for a login link")
        self.call(log_in())
        return "Logged in to Tidal."

    def spotify_client(self) -> SpotifyClient:
        return SpotifyClient(self.config)


EXPLAINED = (AuthenticationError, MissingCredentialsError)
