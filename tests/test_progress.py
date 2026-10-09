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


def test_overall_bar_counts_tracks(manager):
    """overall_add grows the total; overall_advance counts tracks done."""
    progress.overall_add(14)
    progress.overall_advance()
    progress.overall_advance(2)
    task = manager.overall_progress.tasks[0]
    assert task.total == 14
    assert task.completed == 3


def test_overall_total_grows_across_items(manager):
    """An artist's albums each add their tracks to the one bar; the total is
    the running sum, not one album at a time."""
    progress.overall_add(10)  # first album
    progress.overall_add(12)  # second album
    assert manager.overall_progress.tasks[0].total == 22
    assert len(manager.overall_progress.tasks) == 1  # still one bar


def test_overall_add_zero_creates_no_bar(manager):
    """An empty album (everything already downloaded) adds no bar."""
    progress.overall_add(0)
    assert manager.overall_progress.tasks == []
    assert manager._overall_task is None


def test_overall_disabled_adds_nothing(manager):
    progress.overall_add(5, enabled=False)
    progress.overall_advance(enabled=False)
    assert manager.overall_progress.tasks == []


def test_overall_bar_resets_between_runs(manager):
    """clear_progress drops the bar so a second run starts a fresh total."""
    progress.overall_add(14)
    progress.clear_progress()
    assert manager._overall_task is None
    progress.overall_add(5)
    assert manager.overall_progress.tasks[-1].total == 5
    assert manager.overall_progress.tasks[-1].completed == 0


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


def test_overall_total_survives_a_task_removed_in_an_earlier_run(manager):
    """The running total is kept here, not read from progress.tasks[TaskID]:
    that list is reindexed when a task is removed, so a stale TaskID would be
    an out-of-range index (it was, across runs -- an IndexError)."""
    progress.overall_add(3)
    progress.clear_progress()  # removes the task; the TaskID counter moves on
    progress.overall_add(2)
    progress.overall_add(2)  # the update path, where the index was used
    assert manager.overall_progress.tasks[-1].total == 4
