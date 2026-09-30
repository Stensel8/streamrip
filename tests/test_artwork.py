from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from streamrip.config import Config
from streamrip.media import artwork
from streamrip.metadata import Covers


@pytest.mark.asyncio
@pytest.mark.parametrize("for_playlist", [False, True])
@pytest.mark.parametrize("download_fails", [False, True])
async def test_artwork_cleanup_preserves_existing_files(
    tmp_path, monkeypatch, for_playlist, download_fails
):
    folder = tmp_path / "Mix"
    legacy = folder / "__artwork"
    legacy.mkdir(parents=True)
    sentinel = legacy / "keep.txt"
    sentinel.write_text("existing artwork")
    monkeypatch.setattr(artwork, "_artwork_tempdirs", set())
    downloaded = []

    async def download(self, path, callback):
        Path(path).write_bytes(b"image")
        downloaded.append(Path(path))
        if download_fails:
            raise OSError("download interrupted")

    monkeypatch.setattr(artwork.BasicDownloadable, "download", download)
    config = Config.defaults().session.artwork
    config.embed = True
    config.save_artwork = True
    config.embed_max_width = config.saved_max_width = 0

    for _ in range(2):
        covers = Covers()
        covers.set_cover("large", "https://example.test/cover.jpg", None)
        await artwork.download_artwork(
            MagicMock(), str(folder), covers, config, for_playlist
        )

    owned = set(artwork._artwork_tempdirs)
    artwork.remove_artwork_tempdirs()
    assert sentinel.read_text() == "existing artwork"
    assert len(owned) == 2
    assert all(not Path(path).exists() for path in owned)
    assert not artwork._artwork_tempdirs
    assert all(path.parent != legacy for path in downloaded)
    if not for_playlist:
        assert (folder / "cover.jpg").read_bytes() == b"image"

    # A later cleanup must not delete a path recreated after the first cleanup.
    recreated = Path(owned.pop())
    recreated.mkdir()
    artwork.remove_artwork_tempdirs()
    assert recreated.is_dir()


def _flaky_download(monkeypatch, failures):
    """Patch cover downloads to raise each of `failures` once, then succeed."""
    pending = list(failures)
    calls = []

    async def download(self, path, callback):
        calls.append(path)
        if pending:
            raise pending.pop(0)
        Path(path).write_bytes(b"image")

    async def no_sleep(_):
        pass

    monkeypatch.setattr(artwork.BasicDownloadable, "download", download)
    monkeypatch.setattr(artwork.asyncio, "sleep", no_sleep)
    return calls


def _saved_cover_only():
    config = Config.defaults().session.artwork
    config.embed = False
    config.save_artwork = True
    config.saved_max_width = 0
    covers = Covers()
    covers.set_cover("large", "https://example.test/cover.jpg", None)
    return covers, config


@pytest.mark.asyncio
async def test_a_network_hiccup_does_not_cost_the_album_its_cover(
    tmp_path, monkeypatch
):
    calls = _flaky_download(
        monkeypatch, [requests.ConnectionError("Temporary failure in name resolution")]
    )
    covers, config = _saved_cover_only()
    _, saved = await artwork.download_artwork(
        MagicMock(), str(tmp_path), covers, config, False
    )
    assert saved == str(tmp_path / "cover.jpg")
    assert (tmp_path / "cover.jpg").read_bytes() == b"image"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_missing_cover_is_not_retried(tmp_path, monkeypatch):
    not_found = requests.HTTPError(response=MagicMock(status_code=404))
    calls = _flaky_download(monkeypatch, [not_found])
    covers, config = _saved_cover_only()
    result = await artwork.download_artwork(
        MagicMock(), str(tmp_path), covers, config, False
    )
    assert result == (None, None)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_cover_that_keeps_failing_gives_up(tmp_path, monkeypatch):
    calls = _flaky_download(
        monkeypatch,
        [requests.ConnectionError("down")] * artwork.ARTWORK_ATTEMPTS,
    )
    covers, config = _saved_cover_only()
    result = await artwork.download_artwork(
        MagicMock(), str(tmp_path), covers, config, False
    )
    assert result == (None, None)
    assert len(calls) == artwork.ARTWORK_ATTEMPTS
