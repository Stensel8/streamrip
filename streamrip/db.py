"""Wrapper over a database that stores item IDs."""

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger("streamrip")


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

    def __init__(self, path: str):
        """Create a Database instance.

        :param path: Path to the database file.
        """
        assert self.structure != {}
        assert self.name
        assert path

        self.path = path

        if not os.path.exists(self.path):
            self.create()

    def create(self):
        """Create a database."""
        with sqlite3.connect(self.path) as conn:
            params = ", ".join(
                f"{key} {' '.join(map(str.upper, props))} NOT NULL"
                for key, props in self.structure.items()
            )
            command = f"CREATE TABLE {self.name} ({params})"

            logger.debug("executing %s", command)

            conn.execute(command)

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

        with sqlite3.connect(self.path) as conn:
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

        with sqlite3.connect(self.path) as conn:
            try:
                conn.execute(command, tuple(items))
            except sqlite3.IntegrityError as e:
                # tried to insert an item that was already there
                logger.debug(e)

    def remove(self, **items):
        """Delete the rows that match every column-name=value given."""
        conditions = " AND ".join(f"{key}=?" for key in items.keys())
        command = f"DELETE FROM {self.name} WHERE {conditions}"

        with sqlite3.connect(self.path) as conn:
            logger.debug(command)
            conn.execute(command, tuple(items.values()))

    def clear(self) -> int:
        """Delete every row of the table. Returns how many there were."""
        with sqlite3.connect(self.path) as conn:
            count = conn.execute(f"SELECT COUNT(*) FROM {self.name}").fetchone()[0]
            conn.execute(f"DELETE FROM {self.name}")
        return count

    def all(self):
        """Iterate through the rows of the table."""
        with sqlite3.connect(self.path) as conn:
            return list(conn.execute(f"SELECT * FROM {self.name}"))


class Downloads(DatabaseBase):
    """A table that stores the downloaded IDs."""

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
        "id": ["text", "unique"],
    }


@dataclass(slots=True)
class Database:
    downloads: DatabaseBase | Dummy
    failed: DatabaseBase | Dummy
    # Tracks this run downloaded, failed on, and skipped as already downloaded,
    # for the summary at the end of it.
    downloaded_now: int = 0
    failed_now: int = 0
    skipped_now: int = 0

    def downloaded(self, item_id: str) -> bool:
        return self.downloads.contains(id=item_id)

    def set_downloaded(self, item_id: str, new: bool = True):
        """Record a track as in the library; new: downloaded by this run."""
        self.downloads.add((item_id,))
        self.downloaded_now += new

    def set_failed(self, source: str, media_type: str, id: str):
        self.failed.add((source, media_type, id))
        self.failed_now += 1
