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
