import gc
import sqlite3
import warnings

import pytest

from streamrip import db

SOUNDCLOUD_ID = "671678984|https://api-v2.soundcloud.com/media/x/stream/progressive"


@pytest.fixture
def database(tmp_path) -> db.Database:
    return db.Database(
        db.Downloads(str(tmp_path / "downloads.db")),
        db.Failed(str(tmp_path / "failed.db")),
    )


# --- the tables ---------------------------------------------------------------


def test_clear_empties_a_table_that_stays_usable(database):
    downloads = database.downloads
    for key in ("1", "2", "3"):
        downloads.add((key,))

    assert downloads.clear() == 3
    assert downloads.all() == []
    assert downloads.clear() == 0
    downloads.add(("1",))
    assert downloads.contains(id="1")


def test_an_existing_database_file_is_used_as_it_is(tmp_path):
    path = str(tmp_path / "downloads.db")
    sqlite3.connect(path).close()  # a file, but no table in it

    downloads = db.Downloads(path)
    downloads.add(("tidal_1",))
    downloads.create()
    db.Downloads(path).create()  # the table is there now: nothing changes

    assert downloads.all() == [("tidal_1",)]


def test_no_connection_is_left_open(database):
    # `with sqlite3.connect()` leaves its connection open, which Python
    # warns about each time one is garbage collected.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        database.set_downloaded("tidal", "1")
        database.downloaded("tidal", "1")
        database.set_failed("tidal", "track", "1")
        database.forget_failed("tidal", "track", "1")
        database.forget_downloaded("tidal", "1")
        database.downloads.all()
        database.failed.clear()
        gc.collect()

    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


# --- downloads ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "item_id", "found"),
    [
        ("tidal", "123", True),
        ("qobuz", "123", False),  # ids are only unique within a source
        ("tidal", "124", False),
        # A SoundCloud id carries the stream to fetch after a "|", but the track
        # is recorded under the number: looked up whole, it was never found and
        # was downloaded again on every run.
        ("soundcloud", SOUNDCLOUD_ID, True),
        ("soundcloud", "671678984", True),
        ("soundcloud", "671678985|https://x", False),
    ],
)
def test_what_counts_as_downloaded(database, source, item_id, found):
    database.set_downloaded("tidal", "123")
    database.set_downloaded("soundcloud", "671678984")

    assert database.downloaded(source, item_id) is found


@pytest.mark.parametrize(
    ("source", "item_id", "row"),
    [
        ("tidal", "2430924980", "tidal_2430924980"),
        ("soundcloud", SOUNDCLOUD_ID, "soundcloud_671678984"),
    ],
)
def test_a_download_is_recorded_and_forgotten_under_its_key(
    database, source, item_id, row
):
    database.set_downloaded(source, item_id)
    assert database.downloads.all() == [(row,)]

    database.forget_downloaded(source, item_id)
    assert database.downloads.all() == []


def test_set_downloaded_counts_only_new_downloads(database):
    database.set_downloaded("tidal", "1")
    database.set_downloaded("tidal", "2", new=False)

    assert database.downloaded_now == 1


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("tidal_2430924980", ("tidal", "2430924980")),
        ("soundcloud_9", ("soundcloud", "9")),
        ("qobuz_a_b", ("qobuz", "a_b")),  # only the first underscore splits
    ],
)
def test_a_download_key_splits_into_source_and_id(key, expected):
    assert db.split_download_key(key) == expected
    assert db.download_key(*expected) == key


@pytest.mark.parametrize(
    ("source", "item_id", "expected"),
    [
        ("soundcloud", SOUNDCLOUD_ID, "671678984"),
        ("qobuz", "a|b", "a|b"),  # a bar in another source's id is left alone
        ("tidal", 123, "123"),
    ],
)
def test_track_id(source, item_id, expected):
    assert db.track_id(source, item_id) == expected


# --- failures -----------------------------------------------------------------


def test_a_failure_is_one_row_per_source(database):
    database.set_failed("tidal", "track", "42")
    database.set_failed("tidal", "track", "42")  # twice is still one row
    database.set_failed("qobuz", "track", "42")
    database.set_failed("qobuz", "album", "2")
    assert len(database.failed.all()) == 3

    database.forget_failed("tidal", "track", "42")
    assert sorted(database.failed.all()) == [
        ("qobuz", "album", "2"),
        ("qobuz", "track", "42"),
    ]
    database.failed.remove(id="42")  # only the rows matching
    assert database.failed.all() == [("qobuz", "album", "2")]
