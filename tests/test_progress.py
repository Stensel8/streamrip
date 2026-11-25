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


@pytest.mark.parametrize(("size", "total"), [(0, None), (1000, 1000)])
def test_download_bar_total(manager, size, total):
    # Rich takes a total of 0 for a finished task: a full bar on a download
    # that has not started. An unknown size must be None.
    progress.get_progress_callback(True, size, "track")
    task = manager.progress.tasks[0]
    assert task.total == total
    assert not task.finished


def test_a_finished_download_row_is_removed(manager):
    with progress.get_progress_callback(True, 100, "track") as update:
        update(60)
    assert manager.progress.tasks == []
    update(40)  # a download thread's last report, after the row is gone


@pytest.mark.parametrize("name", ["DJ [/] - Mix", "Song [live] [bold]"])
def test_a_track_name_in_a_row_is_text_not_markup(manager, name):
    progress.get_progress_callback(True, 100, name)
    screen = Console(width=120, record=True, force_terminal=False)
    screen.print(manager.progress)
    assert name in screen.export_text()


def test_resolve_row_counts_tracks_and_goes_once_all_are_resolved(manager):
    resolved = progress.get_resolve_callback(True, 3)
    resolved.update(2)
    task = manager.resolve_progress.tasks[0]
    assert (task.completed, task.total, task.visible) == (2, 3, True)
    resolved.update(1)
    assert manager.resolve_progress.tasks == []
    resolved.done()  # again, as leaving its block does


def test_overall_bar_counts_tracks_across_items(manager):
    progress.overall_add(10)  # an artist's albums each add their tracks
    progress.overall_add(12)
    progress.overall_advance()
    progress.overall_advance(2)
    (task,) = manager.overall_progress.tasks
    assert (task.total, task.completed) == (22, 3)


def test_nothing_to_show_adds_no_bar(manager):
    progress.overall_add(0)  # an album with everything downloaded already
    progress.overall_add(5, enabled=False)
    progress.get_progress_callback(False, 1000, "track")
    assert manager.overall_progress.tasks == []
    assert manager.progress.tasks == []


def test_overall_bar_starts_afresh_on_the_next_run(manager):
    progress.overall_add(14)
    progress.clear_progress()
    progress.overall_add(2)
    progress.overall_add(2)  # Rich reindexes its tasks: an old TaskID is out of range
    task = manager.overall_progress.tasks[-1]
    assert (task.total, task.completed) == (4, 0)


def test_a_new_run_after_the_display_was_cleared_starts_it_again(manager):
    progress.get_progress_callback(True, 10, "one")
    progress.clear_progress()
    assert not manager.started
    progress.get_progress_callback(True, 10, "two")
    assert manager.started


def test_the_display_leaves_the_last_column_free(manager):
    # A terminal wraps as soon as the last column is written, and Live does not
    # count the extra line: every redraw would leave the previous rule behind.
    progress.add_title(1, "An album with a title long enough to fill any width " * 4)
    progress.overall_add(3)
    progress.get_progress_callback(True, 1000, "a track " * 30)
    console = Console(width=80, file=None, force_terminal=True)
    lines = console.render_lines(manager._group(), console.options, pad=False)
    assert max(sum(seg.cell_length for seg in line) for line in lines) == 79
