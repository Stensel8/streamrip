import pytest

from streamrip import progress


@pytest.fixture
def manager(monkeypatch):
    """A fresh progress manager in place of the global one; its display is stopped
    afterwards.
    """
    fresh = progress.ProgressManager()
    monkeypatch.setattr(progress, "_p", fresh)
    yield fresh
    if fresh.started:
        fresh.live.stop()


@pytest.mark.parametrize(("size", "total"), [(0, None), (1000, 1000)])
def test_download_bar_total(manager, size, total):
    """A size of 0 is unknown, and must not look finished.

    Rich takes a total of 0 for a finished task: a full bar with no spinner, on a
    download that has not started.
    """
    progress.get_progress_callback(True, size, "track")
    task = manager.progress.tasks[0]
    assert task.total == total
    assert not task.finished


def test_source_bar_keeps_its_total(manager):
    """An album count is never unknown: 0 albums really is a total of 0."""
    progress.get_source_callback(True, 0, "artist")
    assert manager.source_progress.tasks[0].total == 0


def test_disabled_progress_adds_no_task(manager):
    """With progress bars off, no task is added."""
    progress.get_progress_callback(False, 1000, "track")
    assert not manager.progress.tasks


def test_a_new_run_after_the_display_was_cleared_starts_it_again(manager):
    """A cleared display starts again with the next bar.

    Otherwise a second Main in the same process would show no bars at all.
    """
    progress.get_progress_callback(True, 10, "one")
    assert manager.started

    progress.clear_progress()
    assert not manager.started

    progress.get_progress_callback(True, 10, "two")
    assert manager.started
