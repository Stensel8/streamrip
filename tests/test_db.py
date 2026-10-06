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


def test_a_row_from_before_ids_carried_a_source_still_counts_for_every_source(
    tmp_path,
):
    # Nothing says which source a bare id came from. Not matching it would
    # download a whole library again after upgrading.
    database = _database(tmp_path)
    database.downloads.add(("123",))

    assert database.downloaded("tidal", "123")
    assert database.downloaded("qobuz", "123")
    assert not database.downloaded("qobuz", "124")


def test_forgetting_a_track_forgets_its_legacy_row_too(tmp_path):
    database = _database(tmp_path)
    database.downloads.add(("123",))
    database.set_downloaded("tidal", "123")
    database.set_downloaded("qobuz", "123")

    database.forget_downloaded("tidal", "123")

    assert database.downloads.all() == [("qobuz_123",)]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("tidal_2430924980", ("tidal", "2430924980")),
        ("soundcloud_9", ("soundcloud", "9")),
        ("qobuz_a_b", ("qobuz", "a_b")),  # only the first underscore splits
        ("2430924980", (None, "2430924980")),  # legacy
        ("unknown_1", (None, "unknown_1")),  # not one of ours
    ],
)
def test_a_download_key_splits_into_source_and_id(key, expected):
    assert db.split_download_key(key) == expected
    if expected[0]:
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


def _old_failed_database(path, rows):
    """A failed_downloads table as versions before 2.4.10 made it."""
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "CREATE TABLE failed_downloads (source TEXT NOT NULL, "
            "media_type TEXT NOT NULL, id TEXT UNIQUE NOT NULL)"
        )
        conn.executemany("INSERT INTO failed_downloads VALUES (?, ?, ?)", rows)
    conn.close()


def test_an_old_failed_table_is_rebuilt_keeping_its_rows(tmp_path):
    path = str(tmp_path / "failed.db")
    rows = [("tidal", "track", "1"), ("qobuz", "album", "2")]
    _old_failed_database(path, rows)

    failed = db.Failed(path)

    assert sorted(failed.all()) == sorted(rows)
    # Unique per source now: the same id of another source fits beside it.
    failed.add(("qobuz", "track", "1"))
    assert ("qobuz", "track", "1") in failed.all()
    conn = sqlite3.connect(path)
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
    conn.close()
    assert "failed_downloads_old" not in tables


def test_rebuilding_an_old_failed_table_happens_once(tmp_path):
    path = str(tmp_path / "failed.db")
    _old_failed_database(path, [("tidal", "track", "1")])
    db.Failed(path).add(("qobuz", "track", "1"))

    again = db.Failed(path)

    assert sorted(again.all()) == [("qobuz", "track", "1"), ("tidal", "track", "1")]


def test_a_new_failed_table_is_not_rebuilt(tmp_path):
    failed = db.Failed(str(tmp_path / "failed.db"))
    failed.add(("tidal", "track", "1"))
    conn = sqlite3.connect(failed.path)
    before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'failed_downloads'"
    ).fetchone()
    conn.close()

    db.Failed(failed.path)

    conn = sqlite3.connect(failed.path)
    after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'failed_downloads'"
    ).fetchone()
    conn.close()
    assert before == after
    assert "UNIQUE (source, media_type, id)" in after[0]


def test_an_existing_database_without_the_table_gets_it(tmp_path):
    path = str(tmp_path / "downloads.db")
    sqlite3.connect(path).close()  # a file, but nothing in it

    downloads = db.Downloads(path)

    downloads.add(("tidal_1",))
    assert downloads.all() == [("tidal_1",)]


def test_no_connection_is_left_open(tmp_path):
    # `with sqlite3.connect()` leaves its connection open, which Python 3.13
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


def test_a_legacy_row_matches_a_soundcloud_track_by_its_whole_id(tmp_path):
    database = _database(tmp_path)
    database.downloads.add(("671678984",))  # as versions before 2.4.10 wrote it

    assert database.downloaded("soundcloud", SOUNDCLOUD_ID)


def test_forgetting_a_soundcloud_track_by_its_whole_id(tmp_path):
    database = _database(tmp_path)
    database.downloads.add(("671678984",))
    database.set_downloaded("soundcloud", "671678984")

    database.forget_downloaded("soundcloud", SOUNDCLOUD_ID)

    assert database.downloads.all() == []


def test_a_bar_in_another_sources_id_is_left_alone():
    assert db.track_id("soundcloud", SOUNDCLOUD_ID) == "671678984"
    assert db.track_id("qobuz", "a|b") == "a|b"
    assert db.track_id("tidal", 123) == "123"
