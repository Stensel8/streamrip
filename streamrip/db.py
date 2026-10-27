"""The two tables streamrip keeps: what it downloaded, and what failed."""

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
lazy from collections.abc import Generator

logger = logging.getLogger("streamrip")


def track_id(source: str, item_id: str) -> str:
    """The id of a track as the downloads table knows it.

    A SoundCloud id also carries what to fetch, after a "|" (see
    SoundcloudClient._get_custom_id): "1633786176|https://...". The track is
    the number. A track was recorded under the number and looked up under the
    whole id, so it was never found, and downloaded again on every run.
    """
    item_id = str(item_id)
    return item_id.partition("|")[0] if source == "soundcloud" else item_id


def download_key(source: str, item_id: str) -> str:
    """What the downloads table holds for a track: "tidal_2430924980".

    Ids are only unique within a source. Stored bare, a Qobuz track would look
    downloaded as soon as a Tidal track with the same id was.
    """
    return f"{source}_{track_id(source, item_id)}"


def split_download_key(key: str) -> tuple[str, str]:
    """The (source, id) a downloads row holds."""
    source, _, item_id = key.partition("_")
    return source, item_id


class Dummy:
    """Stands in for a table that's disabled in the config: it keeps nothing."""

    def contains(self, **_) -> bool:
        return False

    def add(self, _):
        pass

    def remove(self, **_):
        pass

    def clear(self) -> int:
        return 0

    def all(self) -> list:
        return []


class DatabaseBase:
    """One table of an sqlite database: its name, and its columns' types."""

    name: str
    structure: frozendict[str, tuple[str, ...]]
    # Constraints over several columns, which `structure` cannot say.
    constraints: tuple[str, ...] = ()

    def __init__(self, path: str):
        """The table in the database file at path, made if it isn't there."""
        self.path = path
        self.create()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """A connection that commits (or rolls back) and is then closed.

        `with sqlite3.connect(...)` only ends the transaction: it leaves the
        connection open until it is garbage collected, which Python warns
        about.
        """
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _where(self, items: dict) -> tuple[str, tuple[str, ...]]:
        """The WHERE condition matching every column=value of items, and its
        values. Column names go into the SQL itself, so only the table's own
        are taken."""
        if unknown := set(items) - set(self.structure):
            raise ValueError(f"No column {sorted(unknown)} in {self.name}")
        condition = " AND ".join(f"{key}=?" for key in items)
        return condition, tuple(str(value) for value in items.values())

    def create(self):
        """Create the table, unless the database already has it.

        One statement, so two streamrips starting together cannot both decide
        the table is missing.
        """
        columns = [
            f"{key} {' '.join(map(str.upper, props))} NOT NULL"
            for key, props in self.structure.items()
        ]
        body = ", ".join([*columns, *self.constraints])
        command = f"CREATE TABLE IF NOT EXISTS {self.name} ({body})"
        logger.debug("executing %s", command)
        with self._connect() as conn:
            conn.execute(command)

    def contains(self, **items) -> bool:
        """Whether a row has every column=value given."""
        condition, values = self._where(items)
        command = f"SELECT EXISTS(SELECT 1 FROM {self.name} WHERE {condition})"
        logger.debug("Executing %s", command)
        with self._connect() as conn:
            return bool(conn.execute(command, values).fetchone()[0])

    def add(self, items: tuple[str, ...]):
        """Add a row: a value for every column, in order. A row that is there
        already is left as it is."""
        if len(items) != len(self.structure):
            raise ValueError(
                f"{self.name} has {len(self.structure)} columns, not {len(items)}"
            )
        columns = ", ".join(self.structure)
        marks = ", ".join("?" * len(items))
        command = f"INSERT OR IGNORE INTO {self.name} ({columns}) VALUES ({marks})"
        logger.debug("Executing %s with %s", command, items)
        with self._connect() as conn:
            conn.execute(command, items)

    def remove(self, **items):
        """Delete the rows that have every column=value given."""
        condition, values = self._where(items)
        command = f"DELETE FROM {self.name} WHERE {condition}"
        logger.debug("Executing %s", command)
        with self._connect() as conn:
            conn.execute(command, values)

    def clear(self) -> int:
        """Delete every row of the table. Returns how many there were."""
        with self._connect() as conn:
            count = conn.execute(f"SELECT COUNT(*) FROM {self.name}").fetchone()[0]
            conn.execute(f"DELETE FROM {self.name}")
        return count

    def all(self) -> list[tuple]:
        """Every row of the table."""
        with self._connect() as conn:
            return conn.execute(f"SELECT * FROM {self.name}").fetchall()


class Downloads(DatabaseBase):
    """A table that stores the downloaded IDs, as `download_key`s."""

    name = "downloads"
    structure = frozendict({"id": ("text", "unique")})


class Failed(DatabaseBase):
    """A table that stores information about failed downloads."""

    name = "failed_downloads"
    structure = frozendict(
        {"source": ("text",), "media_type": ("text",), "id": ("text",)}
    )
    # An id is only unique within a source. Unique on its own, one source's
    # failure kept another's, of the same id, out of the table and so out of
    # `streamrip repair`.
    constraints = ("UNIQUE (source, media_type, id)",)


@dataclass(slots=True)
class Database:
    """The downloads and failed-downloads tables, and this run's counts."""

    downloads: DatabaseBase | Dummy
    failed: DatabaseBase | Dummy
    # Tracks this run downloaded, failed on, and skipped as already downloaded,
    # for the summary at the end of it.
    downloaded_now: int = 0
    failed_now: int = 0
    skipped_now: int = 0

    def downloaded(self, source: str, item_id: str) -> bool:
        """Whether a track of `source` is in the library."""
        return self.downloads.contains(id=download_key(source, item_id))

    def set_downloaded(self, source: str, item_id: str, new: bool = True):
        """Record a track as in the library; new: downloaded by this run."""
        self.downloads.add((download_key(source, item_id),))
        self.downloaded_now += new

    def forget_downloaded(self, source: str, item_id: str):
        """Drop a track from the library record."""
        self.downloads.remove(id=download_key(source, item_id))

    def set_failed(self, source: str, media_type: str, item_id: str):
        """Record an item that failed, for `streamrip repair`."""
        self.failed.add((source, media_type, item_id))
        self.failed_now += 1

    def forget_failed(self, source: str, media_type: str, item_id: str):
        """Drop one failure, and no other source's of the same id."""
        self.failed.remove(source=source, media_type=media_type, id=item_id)
