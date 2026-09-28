"""Album downloads: bounded resolving, and one log line for what is already done."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from streamrip.media import album as album_module
from streamrip.media.album import RESOLVE_CONCURRENCY, Album, PendingAlbum


def _album(tracks):
    config = MagicMock()
    config.session.metadata.prefer_explicit = False
    return Album(
        meta=MagicMock(), tracks=tracks, config=config, folder="/x", db=MagicMock()
    )


@pytest.mark.asyncio
async def test_resolves_only_a_few_tracks_at_once():
    concurrent = peak = 0

    class Pending:
        async def resolve(self):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0)
            concurrent -= 1
            track = MagicMock()
            track.rip = AsyncMock()
            return track

    await _album([Pending() for _ in range(RESOLVE_CONCURRENCY * 3)]).download()
    assert 1 < peak <= RESOLVE_CONCURRENCY


@pytest.mark.asyncio
async def test_a_downloading_track_does_not_hold_a_resolve_slot():
    n = RESOLVE_CONCURRENCY * 3
    resolved = 0
    all_resolved = asyncio.Event()

    class Track:
        async def rip(self):
            # Only finishes once every track has been resolved, which can't
            # happen if downloads sit on the few resolve slots.
            await all_resolved.wait()

    class Pending:
        async def resolve(self):
            nonlocal resolved
            await asyncio.sleep(0)
            resolved += 1
            if resolved == n:
                all_resolved.set()
            return Track()

    await asyncio.wait_for(_album([Pending() for _ in range(n)]).download(), 5)
    assert resolved == n


def _pending_album(monkeypatch, tmp_path, tracklist, downloaded):
    meta = MagicMock()
    meta.album = "Encore"
    client = MagicMock()
    client.source = "tidal"
    client.get_metadata = AsyncMock(return_value={})
    db = MagicMock()
    db.downloaded.side_effect = lambda track_id: track_id in downloaded
    config = MagicMock()
    config.session.downloads.folder = str(tmp_path)
    artwork = AsyncMock(return_value=("cover.jpg", None))
    monkeypatch.setattr(
        album_module.AlbumMetadata, "from_album_resp", lambda resp, source: meta
    )
    monkeypatch.setattr(
        album_module, "get_album_track_ids", lambda source, resp: tracklist
    )
    monkeypatch.setattr(album_module, "download_artwork", artwork)
    monkeypatch.setattr(
        album_module, "album_folder", lambda config, source, m: str(tmp_path / "Encore")
    )
    return PendingAlbum("1", client, config, db), artwork


@pytest.mark.asyncio
async def test_finished_album_is_skipped_without_cover_or_folder(
    monkeypatch, tmp_path, caplog
):
    pending, artwork = _pending_album(
        monkeypatch, tmp_path, ["1", "2", "3"], downloaded={"1", "2", "3"}
    )
    with caplog.at_level(logging.INFO, logger="streamrip"):
        album = await pending.resolve()
    assert album.tracks == []
    artwork.assert_not_awaited()
    assert not (tmp_path / "Encore").exists()
    assert "Encore: all 3 tracks already downloaded" in caplog.text


@pytest.mark.asyncio
async def test_partly_downloaded_album_reports_once_and_keeps_the_rest(
    monkeypatch, tmp_path, caplog
):
    pending, artwork = _pending_album(
        monkeypatch, tmp_path, ["1", "2", "3"], downloaded={"1"}
    )
    with caplog.at_level(logging.INFO, logger="streamrip"):
        album = await pending.resolve()
    assert [t.id for t in album.tracks] == ["2", "3"]
    artwork.assert_awaited_once()
    assert (tmp_path / "Encore").is_dir()
    assert "Encore: skipping 1 of 3 tracks already downloaded" in caplog.text
    assert "Skipping track" not in caplog.text


@pytest.mark.asyncio
async def test_new_album_logs_nothing_about_skipping(monkeypatch, tmp_path, caplog):
    pending, _ = _pending_album(monkeypatch, tmp_path, ["1", "2"], downloaded=set())
    with caplog.at_level(logging.INFO, logger="streamrip"):
        album = await pending.resolve()
    assert [t.id for t in album.tracks] == ["1", "2"]
    assert "skipping" not in caplog.text.lower()


class _FakeDownloadable:
    downloaded: list[str] = []

    def __init__(self, _session, url, _extension):
        self.url = url

    async def download(self, path, _callback):
        if "broken" in self.url:
            raise ConnectionError("gone")
        type(self).downloaded.append(self.url)
        with open(path, "wb") as f:
            f.write(b"%PDF")


@pytest.mark.asyncio
async def test_booklets_are_saved_as_pdfs(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(album_module, "BasicDownloadable", _FakeDownloadable)
    _FakeDownloadable.downloaded = []
    (tmp_path / "Liner Notes.pdf").write_bytes(b"%PDF")
    booklets = [
        {"description": "Digital Booklet", "url": "https://q/1.pdf"},
        {"name": "Poster", "url": "https://q/2.jpg"},  # not a booklet
        {"description": "Liner Notes", "url": "https://q/3.pdf"},  # already there
        {"description": "Lyrics", "url": "https://q/broken.pdf"},
        {"description": "Digital Booklet", "url": "https://q/5.pdf"},
    ]

    await album_module.download_booklets(None, booklets, str(tmp_path))

    assert _FakeDownloadable.downloaded == ["https://q/1.pdf", "https://q/5.pdf"]
    # Only names that would collide get a number.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "Digital Booklet 1.pdf",
        "Digital Booklet 4.pdf",
        "Liner Notes.pdf",
    ]
    assert "Could not download booklet https://q/broken.pdf" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_booklets_follow_the_config(monkeypatch, tmp_path, enabled):
    pending, _ = _pending_album(monkeypatch, tmp_path, ["1"], downloaded=set())
    booklets = [{"description": "Booklet", "url": "https://q/1.pdf"}]
    album_module.AlbumMetadata.from_album_resp(None, None).info.booklets = booklets
    pending.config.session.qobuz.download_booklets = enabled
    download = AsyncMock()
    monkeypatch.setattr(album_module, "download_booklets", download)

    await pending.resolve()

    assert download.await_count == (1 if enabled else 0)
