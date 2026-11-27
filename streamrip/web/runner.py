"""streamrip's downloads, searches and logins, run for the web interface.

Downloads are async; the web server is not. They run on an event loop of their
own, in a thread, one download at a time (a second waits for the first, as two
`streamrip` commands would fight over the same accounts' rate limits).
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
lazy from collections.abc import Awaitable, Callable, Coroutine
lazy from typing import Any

from .. import db, progress
from ..accounts import BY_ID
from ..client import (
    CLIENTS,
    DeezerClient,
    QobuzClient,
    SoundcloudClient,
    SpotifyClient,
    TidalClient,
)
from ..console import plural
from ..exceptions import AccountHalt, AuthenticationError
from ..metadata import SearchResults
from ..rip.main import Main
from ..rip.plan import plan_lines
from ..rip.prompter import (
    DeezerPrompter,
    QobuzPrompter,
    SoundcloudPrompter,
    TidalPrompter,
)
from ..rip.repair import repair_downloads
from ..verify import verify as verify_logins
from .jobs import JobLog, JobObserver
lazy from ..config import Config
lazy from .jobs import Job

logger = logging.getLogger("streamrip")

SOURCES = tuple(CLIENTS)
PLANS = ("qobuz", "tidal", "deezer")  # the sources whose login can lower the quality
MEDIA_TYPES = ("track", "album", "artist", "playlist")
SEARCH_RESULTS = 30  # what the page lists of a search


class AccountPausedError(Exception):
    """streamrip stopped for the account's sake (an AccountHalt). That is a
    BaseException, which neither a request nor a job's thread survives: the
    browser got no answer, and the job stayed "running" for ever."""

    http_status = 429


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


# Checks of every login the Accounts page may ask for in CHECK_WINDOW seconds:
# each asks every service, and asking again and again gets an account noticed.
CHECKS = 3
CHECK_WINDOW = 15 * 60


class Runner:
    """The event loop downloads run on, and whose turn it is."""

    def __init__(self, config: Config):
        self.config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Lock()
        self._turn = threading.Lock()  # one download at a time
        # What the last check said of each login, for the Accounts page, with
        # the login it was for; and when the last checks began.
        self._overview: dict[str, tuple[tuple, dict]] = {}
        self._checks: deque[float] = deque(maxlen=CHECKS)

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
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result()
        except AccountHalt as e:
            raise AccountPausedError(str(e)) from None

    # --- downloading -------------------------------------------------------

    @contextlib.contextmanager
    def _working(self, job: Job):
        """Wait for the turn, and show on `job` what streamrip logs and the
        progress of what it downloads."""
        if not self._turn.acquire(blocking=False):
            job.text = "Waiting for the download before this one to finish"
            self._turn.acquire()
        handler = JobLog(job)
        logger.addHandler(handler)
        progress.observe(JobObserver(job))
        # No bars in the terminal the server runs in: the page shows them.
        self.config.session.cli.progress_bars = False
        # A login lowers the quality to what the plan gives, for this run: a
        # server must not keep that after the plan changes.
        asked = {s: getattr(self.config.session, s).quality for s in PLANS}
        try:
            yield
        finally:
            for source, quality in asked.items():
                getattr(self.config.session, source).quality = quality
            progress.observe(None)
            logger.removeHandler(handler)
            self._turn.release()

    def download(self, job: Job, add: Callable[[Main], Awaitable[None]]) -> str:
        """Log in, `add` what to download to a run, and download it, showing
        the progress on `job`. Returns the summary."""
        with self._working(job):
            return self.call(self._run(job, add))

    def plan(self, job: Job, add: Callable[[Main], Awaitable[None]]) -> str:
        """Look up what `add` brings, as a download does, and say what it would
        do (see `--preview`) without downloading anything."""
        with self._working(job):
            items, accounts, summary = self.call(self._plan(job, add))
        for line in [*items, *accounts]:
            job.say(line)
        return summary if items else "Nothing new to download."

    async def _plan(
        self, job: Job, add: Callable[[Main], Awaitable[None]]
    ) -> tuple[list[str], list[str], str]:
        self.config.session.cli.progress_bars = False
        try:
            async with Main(self.config, interactive=False) as main:
                job.enter("find", "Looking everything up")
                await add(main)
                await main.resolve()
                return plan_lines(main)
        finally:
            self.config.save_file()

    def repair(self, job: Job, flat: bool = False) -> str:
        """Retry what failed before (see `streamrip repair`)."""
        with self._working(job):
            job.enter("find", "Retrying the failed downloads")
            failed = db.Failed(
                self.config.session.database.failed_downloads_path
            ).count()
            repaired = self.call(repair_downloads(self.config, flat, interactive=False))
        left = failed - repaired
        return f"Repaired {repaired} of {plural(failed, 'download')}" + (
            f"; {left} failed again and stay in the list." if left else "."
        )

    async def _run(self, job: Job, add: Callable[[Main], Awaitable[None]]) -> str:
        # No bars in the terminal the server runs in: the page shows them.
        self.config.session.cli.progress_bars = False
        try:
            async with Main(self.config, interactive=False) as main:
                job.enter("find", "Logging in and looking everything up")
                await add(main)
                await main.resolve()
                if not main.media:
                    # Already downloaded, or not found: the log says which.
                    return "Nothing new to download; the log says why."
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
            client = await main.search_client(source)
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

    def qobuz_oauth(
        self, redirect: str
    ) -> tuple[str, Callable[[str], tuple[str, str]]]:
        """Qobuz's own login page, which sends the browser to `redirect`, and
        what trades the code it brings for a user id and token."""
        client = QobuzClient(self.config)
        url = self.call(client.oauth_link(redirect))

        def token(code: str) -> tuple[str, str]:
            try:
                return self.call(client.oauth_token(code))
            finally:
                self.call(client.close())

        return url, token

    def login_qobuz(self, user_id: str, token: str):
        """Log in to Qobuz with a user id and token, and keep them if they work."""
        values = {"user_id": user_id, "auth_token": token}
        self.call(self._login(QobuzClient, QobuzPrompter, "qobuz", values))

    def login_soundcloud(self, token: str):
        """Log in to SoundCloud with its oauth_token cookie, and keep it if it works."""
        self.call(
            self._login(
                SoundcloudClient,
                SoundcloudPrompter,
                "soundcloud",
                {"oauth_token": token},
            )
        )

    def _saved(self, login_id: str) -> tuple:
        """What makes the login, as saved now: another value, another login."""
        login = BY_ID[login_id]
        section = getattr(self.config.session, login.section)
        return tuple(getattr(section, name) for group in login.needs for name in group)

    def overview(self) -> dict:
        """What every login is (Free or Paid) and gives, as the last check said.
        Nothing asks the services here; a login saved since is left out."""
        return {
            login_id: answer
            for login_id, (saved, answer) in self._overview.items()
            if saved == self._saved(login_id)
        }

    def check_wait(self) -> float:
        """Seconds until a check may ask every service again: 0 now."""
        if len(self._checks) < CHECKS:
            return 0.0
        return max(CHECK_WINDOW - (time.monotonic() - self._checks[0]), 0.0)

    def verify(self, job: Job) -> str:
        """Ask every service whether its login works; the results go in the log."""
        job.enter("login", "Asking every service")
        self._checks.append(time.monotonic())
        results = self.call(verify_logins(self.config))
        self._overview = {
            r.id: (
                self._saved(r.id),
                {"state": r.state, "plan": r.plan, "formats": r.formats},
            )
            for r in results
        }
        for r in results:
            label = BY_ID[r.id].label
            text = f"{label}: {r.state}" + (f", {r.detail}" if r.detail else "")
            if r.plan or r.formats:
                text += " · " + " · ".join(filter(None, (r.plan, r.formats)))
            level = {"ok": "info", "guest": "info", "failed": "error"}.get(
                r.state, "warning"
            )
            job.say(text, level)
            if r.hint:
                job.say(f"{label}: {r.hint}", "warning")
        good = sum(r.state in {"ok", "guest"} for r in results)
        bad = [BY_ID[r.id].label for r in results if r.state == "failed"]
        summary = f"{good} of {len(results)} are fine"
        return summary + (f"; log in again to {', '.join(bad)}." if bad else ".")

    def login_deezer(self, arl: str):
        """Log in to Deezer with an ARL cookie, and keep it if it works."""
        self.call(self._login(DeezerClient, DeezerPrompter, "deezer", {"arl": arl}))

    async def _login(self, client_type, prompter_type, section: str, values: dict):
        """Try a login in the session's [section], and save it if the source
        takes it. One it refuses leaves the old one in place: the section
        itself stays the same object, which a running download may hold."""
        c = getattr(self.config.session, section)
        before = {name: getattr(c, name) for name in values}
        for name, value in values.items():
            setattr(c, name, value)
        client = client_type(self.config)
        try:
            await client.login()
            if reason := getattr(client, "guest_reason", ""):
                raise AuthenticationError(reason)  # SoundCloud did not take it
        except Exception:
            for name, value in before.items():
                setattr(c, name, value)
            raise
        finally:
            await client.close()
        prompter_type(self.config, client).save()
        self.config.save_file()

    def pair_soloist(self, job: Job, api_key: str) -> str:
        """Pair Spotify's own audio (Soloist) once: Spotify's app on this
        network picks the device. The key is kept only if the pairing works."""
        c = self.config.session.spotify
        job.enter(
            "login",
            "In the Spotify app on a device on this network: press play, tap "
            f"Connect to a device, pick {c.device_name or 'streamrip'} and play a track.",
        )
        before = c.soloist_api_key
        client = SpotifyClient(self.config)

        async def pair() -> str:
            try:
                return await client.pair_soloist(api_key)
            except Exception:
                c.soloist_api_key = before
                raise
            finally:
                await client.close()

        username = self.call(pair())
        self.config.update_logins("spotify", {"soloist_api_key": api_key})
        return f"Signed in to Spotify's own audio as {username}."

    def login_tidal(self, job: Job):
        """Tidal's device login, for downloading: a link to open, once per
        client (twice with the hi-res one)."""

        def show(link: str, minutes: int, which: str):
            job.enter(
                "hires" if which == "hi-res" else "cd",
                f"Open the link and log in to Tidal within {minutes} minutes "
                f"(the {which} login).",
            )
            job.offer(link)

        async def log_in():
            client = TidalClient(self.config)
            prompter = TidalPrompter(self.config, client)
            prompter.show_link = show
            job.steps = ("cd", "hires")[: len(client.lanes())]
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
