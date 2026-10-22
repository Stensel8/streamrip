import gc
import sqlite3
import warnings

import pytest

from streamrip import db


def test_clear_removes_every_row_and_reports_how_many(tmp_path):
    downloads = db.Downloads(str(tmp_path / "downloads.db"))
    for track_id in ("1", "2", "3"):
        downloads.add((track_id,))

    assert downloads.clear() == 3
    assert downloads.all() == []
    assert downloads.clear() == 0


def test_a_cleared_database_is_usable_again(tmp_path):
    downloads = db.Downloads(str(tmp_path / "downloads.db"))
    downloads.add(("1",))
    downloads.clear()

    assert not downloads.contains(id="1")
    downloads.add(("1",))
    assert downloads.contains(id="1")


def test_remove_deletes_only_the_matching_row(tmp_path):
    failed = db.Failed(str(tmp_path / "failed.db"))
    failed.add(("tidal", "track", "1"))
    failed.add(("qobuz", "album", "2"))

    failed.remove(id="1")

    assert failed.all() == [("qobuz", "album", "2")]


def _database(tmp_path) -> db.Database:
    return db.Database(
        db.Downloads(str(tmp_path / "downloads.db")),
        db.Failed(str(tmp_path / "failed.db")),
    )


def test_a_track_is_downloaded_for_its_own_source_only(tmp_path):
    # Ids are only unique within a source: Tidal 123 is not Qobuz 123.
    database = _database(tmp_path)

    database.set_downloaded("tidal", "123")

    assert database.downloaded("tidal", "123")
    assert not database.downloaded("qobuz", "123")
    assert not database.downloaded("tidal", "124")


def test_the_downloads_table_holds_the_source_in_the_id(tmp_path):
    database = _database(tmp_path)

    database.set_downloaded("tidal", "2430924980")

    assert database.downloads.all() == [("tidal_2430924980",)]


def test_set_downloaded_counts_only_new_downloads(tmp_path):
    database = _database(tmp_path)

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


def test_failures_of_different_sources_with_the_same_id_are_both_kept(tmp_path):
    database = _database(tmp_path)

    database.set_failed("tidal", "track", "42")
    database.set_failed("qobuz", "track", "42")

    assert sorted(database.failed.all()) == [
        ("qobuz", "track", "42"),
        ("tidal", "track", "42"),
    ]


def test_forgetting_a_failure_leaves_other_sources_alone(tmp_path):
    database = _database(tmp_path)
    database.set_failed("tidal", "track", "42")
    database.set_failed("qobuz", "track", "42")

    database.forget_failed("tidal", "track", "42")

    assert database.failed.all() == [("qobuz", "track", "42")]


def test_the_same_failure_twice_is_still_one_row(tmp_path):
    database = _database(tmp_path)

    database.set_failed("tidal", "track", "42")
    database.set_failed("tidal", "track", "42")

    assert database.failed.all() == [("tidal", "track", "42")]


def test_an_existing_database_without_the_table_gets_it(tmp_path):
    path = str(tmp_path / "downloads.db")
    sqlite3.connect(path).close()  # a file, but nothing in it

    downloads = db.Downloads(path)

    downloads.add(("tidal_1",))
    assert downloads.all() == [("tidal_1",)]


def test_no_connection_is_left_open(tmp_path):
    # `with sqlite3.connect()` leaves its connection open, which Python
    # warns about each time one is garbage collected.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        database = _database(tmp_path)
        database.set_downloaded("tidal", "1")
        database.downloaded("tidal", "1")
        database.set_failed("tidal", "track", "1")
        database.forget_failed("tidal", "track", "1")
        database.forget_downloaded("tidal", "1")
        database.downloads.all()
        database.failed.clear()
        gc.collect()

    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


def test_creating_the_table_again_changes_nothing(tmp_path):
    downloads = db.Downloads(str(tmp_path / "downloads.db"))
    downloads.add(("tidal_1",))

    downloads.create()
    db.Downloads(downloads.path).create()

    assert downloads.all() == [("tidal_1",)]


SOUNDCLOUD_ID = "671678984|https://api-v2.soundcloud.com/media/x/stream/progressive"


def test_a_soundcloud_track_is_found_under_its_whole_id(tmp_path):
    # Its id carries the stream to fetch after a "|", but the track is recorded
    # under the number alone. Looked up whole, it was never found and was
    # downloaded again on every run.
    database = _database(tmp_path)

    database.set_downloaded("soundcloud", "671678984")

    assert database.downloaded("soundcloud", SOUNDCLOUD_ID)
    assert database.downloaded("soundcloud", "671678984")
    assert not database.downloaded("soundcloud", "671678985|https://x")


def test_a_soundcloud_track_is_recorded_under_the_number_whatever_it_was_called_with(
    tmp_path,
):
    database = _database(tmp_path)

    database.set_downloaded("soundcloud", SOUNDCLOUD_ID)

    assert database.downloads.all() == [("soundcloud_671678984",)]


def test_forgetting_a_soundcloud_track_by_its_whole_id(tmp_path):
    database = _database(tmp_path)
    database.set_downloaded("soundcloud", "671678984")

    database.forget_downloaded("soundcloud", SOUNDCLOUD_ID)

    assert database.downloads.all() == []


def test_a_bar_in_another_sources_id_is_left_alone():
    assert db.track_id("soundcloud", SOUNDCLOUD_ID) == "671678984"
    assert db.track_id("qobuz", "a|b") == "a|b"
    assert db.track_id("tidal", 123) == "123"
