"""Wrapper over a database that stores item IDs."""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger("streamrip")

# The sources a download key can start with.
SOURCES: Final = ("qobuz", "tidal", "deezer", "soundcloud")


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


def split_download_key(key: str) -> tuple[str | None, str]:
    """The (source, id) a downloads row holds.

    The source is None for a row from before ids carried one.
    """
    source, separator, item_id = key.partition("_")
    if separator and source in SOURCES:
        return source, item_id
    return None, key


class Dummy:
    """Stands in for a database that's disabled in the config."""

    def contains(self, **_) -> bool:
        """Always return False: nothing is ever stored."""
        return False

    def add(self, *_):
        pass

    def remove(self, **_):
        """No-op: there is nothing to remove."""
        pass

    def clear(self) -> int:
        """No-op: there is nothing to clear."""
        return 0

    def all(self) -> list:
        """Return an empty list: there is nothing stored."""
        return []


class DatabaseBase:
    """A wrapper for an sqlite database."""

    structure: dict
    name: str
    # Constraints over several columns, which `structure` cannot say.
    constraints: tuple[str, ...] = ()

    def __init__(self, path: str):
        """Create a Database instance.

        :param path: Path to the database file.
        """
        assert self.structure != {}
        assert self.name
        assert path

        self.path = path

        self.create()
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits (or rolls back) and is then closed.

        `with sqlite3.connect(...)` only ends the transaction: it leaves the
        connection open until it is garbage collected, which Python 3.13 warns
        about.
        """
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _create_command(self, name: str, *, if_not_exists: bool = False) -> str:
        """The statement that creates this table as `name`."""
        columns = [
            f"{key} {' '.join(map(str.upper, props))} NOT NULL"
            for key, props in self.structure.items()
        ]
        guard = "IF NOT EXISTS " if if_not_exists else ""
        body = ", ".join([*columns, *self.constraints])
        return f"CREATE TABLE {guard}{name} ({body})"

    def create(self):
        """Create the table, unless the database already has it.

        One statement, so two streamrips starting together cannot both decide
        the table is missing.
        """
        command = self._create_command(self.name, if_not_exists=True)
        logger.debug("executing %s", command)
        with self._connect() as conn:
            conn.execute(command)

    def migrate(self):
        """Bring a table made by an older version up to date. Nothing to do here."""

    def contains(self, **items) -> bool:
        """Check whether items matches an entry in the table.

        :param items: a dict of column-name + expected value
        :rtype: bool
        """
        allowed_keys = set(self.structure.keys())
        assert all(key in allowed_keys for key in items.keys()), (
            f"Invalid key. Valid keys: {allowed_keys}"
        )

        items = {k: str(v) for k, v in items.items()}

        with self._connect() as conn:
            conditions = " AND ".join(f"{key}=?" for key in items.keys())
            command = f"SELECT EXISTS(SELECT 1 FROM {self.name} WHERE {conditions})"

            logger.debug("Executing %s", command)

            return bool(conn.execute(command, tuple(items.values())).fetchone()[0])

    def add(self, items: tuple[str]):
        """Add a row to the table.

        :param items: Column-name + value. Values must be provided for all cols.
        :type items: Tuple[str]
        """
        assert len(items) == len(self.structure)

        params = ", ".join(self.structure.keys())
        question_marks = ", ".join("?" for _ in items)
        command = f"INSERT INTO {self.name} ({params}) VALUES ({question_marks})"

        logger.debug("Executing %s", command)
        logger.debug("Items to add: %s", items)

        with self._connect() as conn:
            try:
                conn.execute(command, tuple(items))
            except sqlite3.IntegrityError as e:
                # tried to insert an item that was already there
                logger.debug(e)

    def remove(self, **items):
        """Delete the rows that match every column-name=value given."""
        conditions = " AND ".join(f"{key}=?" for key in items.keys())
        command = f"DELETE FROM {self.name} WHERE {conditions}"

        with self._connect() as conn:
            logger.debug(command)
            conn.execute(command, tuple(items.values()))

    def clear(self) -> int:
        """Delete every row of the table. Returns how many there were."""
        with self._connect() as conn:
            count = conn.execute(f"SELECT COUNT(*) FROM {self.name}").fetchone()[0]
            conn.execute(f"DELETE FROM {self.name}")
        return count

    def all(self):
        """Iterate through the rows of the table."""
        with self._connect() as conn:
            return list(conn.execute(f"SELECT * FROM {self.name}"))


class Downloads(DatabaseBase):
    """A table that stores the downloaded IDs, as `download_key`s."""

    name = "downloads"
    structure: Final[dict] = {
        "id": ["text", "unique"],
    }


class Failed(DatabaseBase):
    """A table that stores information about failed downloads."""

    name = "failed_downloads"
    structure: Final[dict] = {
        "source": ["text"],
        "media_type": ["text"],
        "id": ["text"],
    }
    # An id is only unique within a source. Unique on its own, one source's
    # failure kept another's, of the same id, out of the table and so out of
    # `streamrip repair`.
    constraints = ("UNIQUE (source, media_type, id)",)

    @staticmethod
    def _unique_on_id_alone(conn: sqlite3.Connection, table: str) -> bool:
        """Whether `table` is the old one, unique on its id column only."""
        for _, index, unique, *_ in conn.execute(f"PRAGMA index_list({table})"):
            if unique:
                columns = [
                    row[2] for row in conn.execute(f"PRAGMA index_info({index})")
                ]
                if columns == ["id"]:
                    return True
        return False

    def migrate(self):
        """Rebuild a table that is unique on the id alone, keeping its rows."""
        with self._connect() as conn:
            if not self._unique_on_id_alone(conn, self.name):
                return
            conn.execute("BEGIN IMMEDIATE")
            if not self._unique_on_id_alone(conn, self.name):
                return  # another streamrip got here first
            logger.debug("Rebuilding %s: unique per source now", self.name)
            columns = ", ".join(self.structure)
            conn.execute(f"ALTER TABLE {self.name} RENAME TO {self.name}_old")
            conn.execute(self._create_command(self.name))
            conn.execute(
                f"INSERT OR IGNORE INTO {self.name} ({columns}) "
                f"SELECT {columns} FROM {self.name}_old"
            )
            conn.execute(f"DROP TABLE {self.name}_old")


@dataclass(slots=True)
class Database:
    downloads: DatabaseBase | Dummy
    failed: DatabaseBase | Dummy
    # Tracks this run downloaded, failed on, and skipped as already downloaded,
    # for the summary at the end of it.
    downloaded_now: int = 0
    failed_now: int = 0
    skipped_now: int = 0

    def downloaded(self, source: str, item_id: str) -> bool:
        """Whether a track of `source` is in the library.

        A row from before ids carried their source holds the bare id and
        cannot say which source it was from. It still counts, for any of them,
        so upgrading does not download everything again.
        """
        return self.downloads.contains(
            id=download_key(source, item_id)
        ) or self.downloads.contains(id=track_id(source, item_id))

    def set_downloaded(self, source: str, item_id: str, new: bool = True):
        """Record a track as in the library; new: downloaded by this run."""
        self.downloads.add((download_key(source, item_id),))
        self.downloaded_now += new

    def forget_downloaded(self, source: str, item_id: str):
        """Drop a track from the library record, also as a bare legacy id."""
        self.downloads.remove(id=download_key(source, item_id))
        self.downloads.remove(id=track_id(source, item_id))

    def set_failed(self, source: str, media_type: str, id: str):
        self.failed.add((source, media_type, id))
        self.failed_now += 1

    def forget_failed(self, source: str, media_type: str, id: str):
        """Drop one failure, and no other source's of the same id."""
        self.failed.remove(source=source, media_type=media_type, id=id)
