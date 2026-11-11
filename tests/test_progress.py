import pytest
from rich.console import Console

from streamrip import progress


@pytest.fixture
def manager(monkeypatch):
    """A fresh progress manager in place of the global one, stopped afterwards."""
    fresh = progress.ProgressManager()
    monkeypatch.setattr(progress, "_p", fresh)
    yield fresh
    if fresh.started:
        fresh.live.stop()


# --- download and resolve rows ------------------------------------------------


@pytest.mark.parametrize(("size", "total"), [(0, None), (1000, 1000)])
def test_download_bar_total(manager, size, total):
    # A size of 0 is unknown. Rich takes a total of 0 for a finished task: a
    # full bar with no spinner, on a download that has not started.
    progress.get_progress_callback(True, size, "track")
    task = manager.progress.tasks[0]
    assert task.total == total
    assert not task.finished


def test_a_finished_download_row_is_removed_not_kept_hidden(manager):
    with progress.get_progress_callback(True, 100, "track") as update:
        update(60)
    assert manager.progress.tasks == []
    update(40)  # a download thread's last report, after the row is gone


def test_resolve_row_counts_tracks_and_goes_once_all_are_resolved(manager):
    # Looking up an album's tracks shows, until the last one is looked up.
    resolved = progress.get_resolve_callback(True, 3)
    resolved.update(2)
    task = manager.resolve_progress.tasks[0]
    assert (task.completed, task.total, task.visible) == (2, 3, True)
    resolved.update(1)
    assert manager.resolve_progress.tasks == []
    resolved.done()  # again, as leaving its block does


# --- the overall bar ----------------------------------------------------------


def test_overall_bar_counts_tracks_across_items(manager):
    # An artist's albums each add their tracks to the one bar.
    progress.overall_add(10)
    progress.overall_add(12)
    progress.overall_advance()
    progress.overall_advance(2)
    assert len(manager.overall_progress.tasks) == 1
    task = manager.overall_progress.tasks[0]
    assert (task.total, task.completed) == (22, 3)


def test_nothing_to_show_adds_no_bar(manager):
    progress.overall_add(0)  # an album with everything downloaded already
    progress.overall_add(5, enabled=False)
    progress.overall_advance(enabled=False)
    progress.get_progress_callback(False, 1000, "track")
    assert manager.overall_progress.tasks == []
    assert manager.progress.tasks == []


def test_overall_bar_starts_afresh_on_the_next_run(manager):
    progress.overall_add(14)
    progress.clear_progress()  # removes the task; the TaskID counter moves on
    progress.overall_add(2)
    # The update path: Rich's task list is reindexed when a task is removed,
    # so a TaskID from the earlier run was an out-of-range index there.
    progress.overall_add(2)
    task = manager.overall_progress.tasks[-1]
    assert (task.total, task.completed) == (4, 0)


# --- the display --------------------------------------------------------------


def test_a_new_run_after_the_display_was_cleared_starts_it_again(manager):
    # Otherwise a second Main in the same process would show no bars at all.
    progress.get_progress_callback(True, 10, "one")
    assert manager.started

    progress.clear_progress()
    assert not manager.started

    progress.get_progress_callback(True, 10, "two")
    assert manager.started


@pytest.mark.parametrize("width", [40, 80, 160])
def test_the_display_leaves_the_last_column_free(manager, width):
    # A terminal that wraps as soon as the last column is written turns a
    # full-width line into two, which Live does not count: each redraw then
    # leaves the previous "Downloading" rule behind.
    progress.add_title(1, "An album with a title long enough to fill any width " * 4)
    progress.overall_add(3)
    progress.get_progress_callback(True, 1000, "a track " * 30)
    console = Console(width=width, file=None, force_terminal=True)
    lines = console.render_lines(manager._group(), console.options, pad=False)
    assert lines
    assert max(sum(seg.cell_length for seg in line) for line in lines) == width - 1
