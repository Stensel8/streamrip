"""Background jobs, so a long download, transfer or login does not hold up a
browser request. The page polls each job for how it is getting on."""

import itertools
import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any
lazy from collections.abc import Callable

import aiohttp

from ..progress import NO_PROGRESS, Handle, Observer
from ..sync.engine import Miss, Step, remaining
lazy from ..sync.models import Track

logger = logging.getLogger("streamrip")

# The steps each kind of job goes through, in order. The page shows them as a
# row of numbered steps.
PHASES = frozendict(
    {
        "export": ("read",),
        "import": ("check", "match", "add"),
        "transfer": ("read", "check", "match", "add"),
        "download": ("find", "download"),
        "playlist-download": ("read", "find", "download"),
        "login": ("login",),
        "verify": ("login",),
        "plan": ("find",),
        "repair": ("find", "download"),
    }
)
SHOWN_MISSES = 50  # tracks not found that the page lists; the CSV has them all
LOG_LINES = 200  # of a download's log, the last this many


@dataclass(slots=True)
class Row:
    """One track downloading, or the tracks being looked up."""

    desc: str
    total: int | None
    done: int = 0


@dataclass(slots=True)
class Job:
    id: str
    kind: str  # one of PHASES
    title: str  # "Transfer from Spotify to Tidal"
    status: str = "running"  # running, done or error
    phase: str | None = None
    text: str = "Starting"  # what is happening now
    done: int = 0
    total: int | None = None  # None while it is not known
    current: str = ""  # the track being looked up
    found: int = 0
    misses: list[Miss] = field(default_factory=list)  # tracks not found, and why
    message: str = ""  # the summary, or what went wrong
    # An export's file name and tracks, once it is done.
    download: tuple[str, list[Track]] | None = None
    # A download's tracks in flight, and the last lines of its log.
    rows: dict[int, Row] = field(default_factory=dict)
    log: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    titles: dict[int, str] = field(default_factory=dict)
    # A login's link to open, and whether the address the browser ends up on
    # can be pasted (a browser login, not Tidal's device login).
    link: str = ""
    pasteable: bool = False
    started: float = field(default_factory=time.monotonic)
    phase_started: float = field(default_factory=time.monotonic)
    finished: float | None = None

    def enter(self, phase: str, text: str | None = None):
        """Go on to `phase` (if not already there), saying `text`."""
        if phase != self.phase:
            self.phase, self.phase_started, self.current = phase, time.monotonic(), ""
            self.done, self.total = 0, None
        if text is not None:
            self.text = text

    def progress(self, step: Step):
        """The progress callback of the playlist tools. The page polls what it sets."""
        self.enter(step.phase)
        self.text, self.done, self.total = step.text, step.done, step.total
        if step.phase == "match" and step.track:
            self.current, self.found = str(step.track), step.found
            if step.miss:
                self.misses.append(step.miss)

    def to_json(self) -> dict[str, Any]:
        """Everything the page shows about the job."""
        now = self.finished or time.monotonic()
        eta = (
            remaining(self.done, self.total, now - self.phase_started)
            if self.status == "running"
            else None
        )
        return {
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "phases": PHASES[self.kind],
            "phase": self.phase,
            "text": self.text,
            "done": self.done,
            "total": self.total,
            "current": self.current,
            "found": self.found,
            "not_found": len(self.misses),
            "elapsed": round(now - self.started, 1),
            "eta": None if eta is None else round(eta),
            "message": self.message,
            "unmatched": [
                {"track": str(miss.track), "why": miss.why}
                for miss in self.misses[:SHOWN_MISSES]
            ],
            "download": self.download is not None,
            "rows": [
                {"desc": row.desc, "done": row.done, "total": row.total}
                for row in list(self.rows.values())
            ],
            "titles": list(self.titles.values()),
            "log": [{"level": level, "text": text} for level, text in list(self.log)],
            "link": self.link,
            "pasteable": self.pasteable,
        }


class JobObserver(Observer):
    """Shows a download's progress on its job: the tracks to do and done, and
    a row per track in flight."""

    def __init__(self, job: Job):
        self.job = job
        self._ids = itertools.count()

    def row(self, kind: str, desc: str, total: int | None) -> Handle:
        if kind != "download":
            return NO_PROGRESS  # the look-ups show as the "find" step
        key = next(self._ids)
        row = self.job.rows[key] = Row(desc, total)

        def update(n: int):
            row.done += n

        def done():
            self.job.rows.pop(key, None)

        return Handle(update, done)

    def overall_add(self, n: int):
        job = self.job
        if job.phase != "download":
            job.enter("download", "Downloading")
        job.total = (job.total or 0) + n

    def overall_advance(self, n: int):
        self.job.done += n

    def title(self, key: int, title: str | None):
        if title is None:
            self.job.titles.pop(key, None)
        else:
            self.job.titles[key] = title
        names = list(self.job.titles.values())
        if self.job.phase == "download" and len(names) == 1:
            self.job.text = f"Downloading {names[0]}"


class JobLog(logging.Handler):
    """The lines streamrip logs while a download runs, kept on its job."""

    def __init__(self, job: Job):
        super().__init__(logging.INFO)
        self.job = job

    def emit(self, record: logging.LogRecord):
        try:
            self.job.log.append((record.levelname.lower(), record.getMessage()))
        except Exception:
            self.handleError(record)


def explain(e: BaseException) -> str:
    """What went wrong, for people."""
    if isinstance(e, aiohttp.ClientResponseError) and e.request_info:
        return f"{e.request_info.url.host} answered HTTP {e.status} ({e.message})."
    return str(e) or type(e).__name__


class JobManager:
    """Runs jobs on threads and remembers the most recent ones."""

    def __init__(self, explained: tuple[type[BaseException], ...] = (), keep: int = 30):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._keep = keep
        # Failures whose message is for the user; anything else is a bug.
        self._explained = explained

    def start(self, kind: str, title: str, work: Callable[[Job], str]) -> Job:
        """Run `work(job)` on a thread. What it returns is the summary shown
        once the job is done."""
        job = Job(id=secrets.token_urlsafe(8), kind=kind, title=title)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)  # forget the oldest
        threading.Thread(target=self._run, args=(job, work), daemon=True).start()
        return job

    def _run(self, job: Job, work: Callable[[Job], str]):
        try:
            job.message, status = work(job), "done"
        except self._explained as e:
            job.message, status = explain(e), "error"
        except Exception:
            # A bug: the details go to the terminal, not to the browser.
            logger.exception("Job %s failed", job.id)
            job.message, status = (
                "Unexpected error; see the terminal for details.",
                "error",
            )
        job.rows.clear()
        job.finished = time.monotonic()
        job.status = status  # last: everything else is set before the page sees it end

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running(self) -> Job | None:
        """The newest job that is still going, for a page that was reloaded."""
        with self._lock:
            return next(
                (j for j in reversed(self._jobs.values()) if j.status == "running"),
                None,
            )
