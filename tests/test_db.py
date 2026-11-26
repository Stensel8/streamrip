import gc
import sqlite3
import warnings
from contextlib import closing

import pytest

from streamrip import db

SOUNDCLOUD_ID = "671678984|https://api-v2.soundcloud.com/media/x/stream/progressive"


@pytest.fixture
def database(tmp_path) -> db.Database:
    return db.Database(
        db.Downloads(str(tmp_path / "downloads.db")),
        db.Failed(str(tmp_path / "failed.db")),
    )


def test_clear_empties_a_table_that_stays_usable(database):
    for key in ("2", "3", "1"):  # the last added, not the highest
        database.downloads.add((key, ""))
    assert database.downloads.count() == 3
    assert database.downloads.recent(2) == [("1", ""), ("3", "")]

    assert database.downloads.clear() == 3
    assert database.downloads.all() == []
    database.downloads.add(("1", ""))
    assert database.downloads.contains(id="1")


def test_no_connection_is_left_open(database):
    # `with sqlite3.connect()` leaves its connection open, which Python warns
    # about each time one is garbage collected.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        database.set_downloaded("tidal", "1")
        database.downloaded("tidal", "1")
        database.set_failed("tidal", "track", "1")
        database.forget_failed("tidal", "track", "1")
        gc.collect()

    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


@pytest.mark.parametrize(
    ("source", "item_id", "found"),
    [
        ("tidal", "123", True),
        ("qobuz", "123", False),  # ids are only unique within a source
        # A SoundCloud id carries the stream to fetch after a "|", but the track
        # is recorded under the number: looked up whole, it was downloaded again
        # on every run.
        ("soundcloud", SOUNDCLOUD_ID, True),
        ("soundcloud", "671678985|https://x", False),
    ],
)
def test_what_counts_as_downloaded(database, source, item_id, found):
    database.set_downloaded("tidal", "123")
    database.set_downloaded("soundcloud", "671678984")

    assert database.downloaded(source, item_id) is found


def test_a_track_that_came_through_is_no_failure_any_more(database):
    # `repair` fetches every failed track again: one that failed once and
    # came through later must not be downloaded a second time.
    database.set_failed("tidal", "track", "1")
    database.set_failed("qobuz", "track", "1")  # another source, same id
    database.set_failed("soundcloud", "track", "5|https://api.test/x")

    database.set_downloaded("tidal", "1")
    database.set_downloaded("soundcloud", "5|https://api.test/x")

    assert database.failed.all() == [("qobuz", "track", "1")]


def test_a_failure_is_one_row_per_source(database):
    database.set_failed("tidal", "track", "42")
    database.set_failed("tidal", "track", "42")  # twice is still one row
    database.set_failed("qobuz", "track", "42")
    assert len(database.failed.all()) == 2

    database.forget_failed("tidal", "track", "42")
    assert database.failed.all() == [("qobuz", "track", "42")]


def test_a_database_in_a_folder_that_is_not_there_makes_the_folder(tmp_path):
    # A config copied to another machine, or into a container, points at the
    # old one's folder.
    path = tmp_path / "home" / "someone" / ".config" / "streamrip" / "downloads.db"

    database = db.Downloads(str(path))
    database.add(("an-id", ""))

    assert path.is_file()
    assert database.contains(id="an-id")


def test_a_downloads_table_from_before_files_gets_the_column(tmp_path):
    path = tmp_path / "downloads.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE downloads (id TEXT UNIQUE NOT NULL)")
        conn.execute("INSERT INTO downloads VALUES ('qobuz_1')")
    table = db.Downloads(str(path))
    table.add(("qobuz_2", "/music/a.flac"))
    assert table.all() == [("qobuz_1", ""), ("qobuz_2", "/music/a.flac")]
