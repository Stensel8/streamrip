"""concat_audio_files: bounded ffmpeg processes, and cleanup on failure or cancel."""

import asyncio

import pytest

from streamrip.client.downloadable import concat_audio_files


class _Proc:
    """An ffmpeg process that runs until the test lets it finish (or kills it)."""

    def __init__(self, ffmpeg, index, outpath):
        self.ffmpeg = ffmpeg
        self.index = index
        self.returncode = None
        self.killed = False
        # ffmpeg opens its output as soon as it starts.
        open(outpath, "wb").close()
        ffmpeg.active += 1
        ffmpeg.max_active = max(ffmpeg.max_active, ffmpeg.active)

    def _exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self.ffmpeg.active -= 1

    async def communicate(self):
        if self.index in self.ffmpeg.failing:
            self._exit(1)
            return b"", b"boom"
        await self.ffmpeg.gate.wait()
        self._exit(0)
        return b"", b""

    def kill(self):
        self.killed = True
        self._exit(-9)

    async def wait(self):
        return self.returncode


class _Ffmpeg:
    """Replaces asyncio.create_subprocess_exec for the ffmpeg processes."""

    def __init__(self, failing=(), fail_to_start=None):
        self.gate = asyncio.Event()  # set to let the running processes finish
        self.failing = failing  # indexes of the processes that exit with 1
        self.fail_to_start = fail_to_start  # the call that raises OSError
        self.calls = 0
        self.procs = []
        self.active = 0
        self.max_active = 0

    async def create(self, *args, **_kwargs):
        self.calls += 1
        if self.calls == self.fail_to_start:
            raise OSError("cannot start ffmpeg")
        index = self.calls
        await asyncio.sleep(0)  # starting a process takes a moment
        proc = _Proc(self, index, args[-1])
        self.procs.append(proc)
        return proc


@pytest.fixture
def temp(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    monkeypatch.setattr(
        "streamrip.client.downloadable.find_ffmpeg", lambda: "/usr/bin/ffmpeg"
    )
    return temp


@pytest.fixture
def ffmpeg(monkeypatch, temp):
    def make(**kwargs):
        fake = _Ffmpeg(**kwargs)
        monkeypatch.setattr(
            "streamrip.client.downloadable.asyncio.create_subprocess_exec",
            fake.create,
        )
        return fake

    return make


async def _until(condition):
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never became true")


async def _concat(*args, **kwargs):
    # A regression that leaves processes running would wait forever: fail instead.
    async with asyncio.timeout(5):
        await concat_audio_files(*args, **kwargs)


PATHS = [f"segment{n}.mp3" for n in range(8)]  # 4 batches of 2


async def test_batches_are_joined_into_one_file(ffmpeg, temp, tmp_path):
    fake = ffmpeg()
    fake.gate.set()
    out = tmp_path / "out.mp3"

    await _concat(PATHS, str(out), "mp3", max_files_open=2)

    assert out.exists()
    assert list(temp.iterdir()) == []
    # Four batches of two, then the four files they made, joined at once.
    assert len(fake.procs) == 5


async def test_at_most_max_processes_run_at_once(ffmpeg, temp, tmp_path):
    fake = ffmpeg()
    task = asyncio.create_task(
        _concat(
            PATHS, str(tmp_path / "out.mp3"), "mp3", max_files_open=2, max_processes=2
        )
    )

    await _until(lambda: len(fake.procs) == 2)
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(fake.procs) == 2  # the other batches wait for a free slot

    fake.gate.set()
    await task

    assert fake.max_active == 2
    assert len(fake.procs) == 5
    assert list(temp.iterdir()) == []


async def test_cancelling_kills_the_processes_and_removes_their_files(
    ffmpeg, temp, tmp_path
):
    fake = ffmpeg()
    task = asyncio.create_task(
        _concat(
            PATHS, str(tmp_path / "out.mp3"), "mp3", max_files_open=2, max_processes=2
        )
    )
    await _until(lambda: len(fake.procs) == 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(fake.procs) == 2  # the ones waiting for a slot never started
    assert all(proc.killed for proc in fake.procs)
    assert list(temp.iterdir()) == []


async def test_a_process_that_cannot_start_stops_the_ones_already_started(
    ffmpeg, temp, tmp_path
):
    fake = ffmpeg(fail_to_start=3)

    with pytest.raises(OSError, match="cannot start ffmpeg"):
        await _concat(PATHS, str(tmp_path / "out.mp3"), "mp3", max_files_open=2)

    assert fake.procs
    assert all(proc.killed for proc in fake.procs)
    assert list(temp.iterdir()) == []


async def test_a_failing_process_stops_the_others(ffmpeg, temp, tmp_path):
    fake = ffmpeg(failing={1})

    with pytest.raises(Exception, match="FFMPEG returned with status code 1: boom"):
        await _concat(PATHS, str(tmp_path / "out.mp3"), "mp3", max_files_open=2)

    failed, *others = fake.procs
    assert failed.returncode == 1
    assert not failed.killed  # it had already exited
    assert others
    assert all(proc.killed for proc in others)
    assert list(temp.iterdir()) == []
