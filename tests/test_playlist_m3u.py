"""Playlists as M3U8 files that point at the music of a library (Music-Sync #11)."""

import shutil
from pathlib import Path

import mutagen
import pytest
from playlist_support import (
    ISRC_A,
    ISRC_B,
    FakeProvider,
    FakeServices,
    config_at,
    track,
)

from streamrip.rip import playlist_cli
from streamrip.sync.csvio import read_tracks
from streamrip.sync.m3u import Library, read_file, write_m3u8

HERE = Path(__file__).parent


def tagged(folder: Path, name: str, source: str, **tags) -> Path:
    """A copy of a silent test file, with these tags (easy names)."""
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / source, path)
    audio = mutagen.File(path, easy=True)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


@pytest.fixture
def library(tmp_path) -> Path:
    music = tmp_path / "Music"
    # One recording on two releases: the older one is taken, whose file name
    # says nothing (ripping tools rename): the ISRC finds it.
    tagged(
        music,
        "Various/Hits/07. Song A.flac",
        "silence.flac",
        title="Song A",
        artist="Artist A",
        isrc=ISRC_A,
        date="2020",
    )
    tagged(
        music,
        "Artist A/Album/01. sng a (Explicit).flac",
        "silence.flac",
        title="Song A",
        artist="Artist A",
        isrc=ISRC_A,
        date="2001",
    )
    # No ISRC (an M4A): found by title and artist.
    tagged(
        music,
        "Artist B/02. Song B.m4a",
        "silence.m4a",
        title="Song B",
        artist="Artist B",
    )
    (music / "cover.jpg").write_bytes(b"not audio")
    return music


def test_the_tags_of_each_format_are_read(tmp_path):
    flac = tagged(
        tmp_path,
        "a.flac",
        "silence.flac",
        title="T",
        artist="X",
        isrc="us-aaa-00-00001",
    )
    m4a = tagged(tmp_path, "b.m4a", "silence.m4a", title="U", artist="Y", isrc=ISRC_B)
    assert read_file(flac).isrc == ISRC_A  # in its canonical form
    # MP4 keeps it in a freeform atom, as streamrip's tagger writes it.
    assert (read_file(m4a).title, read_file(m4a).isrc) == ("U", ISRC_B)
    assert read_file(HERE / "1x1_pixel.jpg") is None


def test_a_playlist_points_at_the_files_of_the_library(tmp_path, library):
    music = Library.scan(library)
    assert music.size == 3
    wanted = [
        track("Song A", "Artist A", isrc=ISRC_A),
        track("Song B", "Artist B"),
        track("Not here", "Nobody"),
    ]
    playlist = tmp_path / "Music" / "Playlists" / "Mix.m3u8"
    written, missing = write_m3u8(playlist, wanted, music)
    assert (written, [t.title for t in missing]) == (2, ["Not here"])
    lines = playlist.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1].startswith("#EXTINF:")
    assert lines[1].endswith(",Artist A - Song A")
    # Relative to the playlist, so the library can be mounted elsewhere.
    assert lines[2] == "../Artist A/Album/01. sng a (Explicit).flac"
    assert lines[4] == "../Artist B/02. Song B.m4a"

    write_m3u8(playlist, wanted[:1], music, absolute=True)
    assert playlist.read_text(encoding="utf-8").splitlines()[2] == str(
        (library / "Artist A/Album/01. sng a (Explicit).flac").resolve()
    )


def test_without_a_library_the_playlist_only_describes_the_tracks(tmp_path):
    path = tmp_path / "x.m3u8"
    written, missing = write_m3u8(
        path, [track("Song A", "Artist A", album="Album", duration_ms=200_000)]
    )
    assert (written, missing) == (1, [])
    assert (
        path.read_text(encoding="utf-8")
        == "#EXTM3U\n#EXTINF:200,Artist A - Song A\n#EXTALB:Album\n"
    )


def test_export_writes_m3u8_playlists_and_what_the_library_lacks(
    tmp_path, library, monkeypatch, capsys
):
    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    spotify = FakeProvider(
        "spotify",
        liked=[track("Song A", "Artist A", isrc=ISRC_A), track("Gone", "Nobody")],
    )
    spotify.existing_playlist("Road trip", track("Song B", "Artist B"))
    monkeypatch.setattr(
        playlist_cli, "_services", lambda _ctx: FakeServices(tmp_path, spotify=spotify)
    )
    monkeypatch.setenv("STREAMRIP_NO_UPDATE_CHECK", "1")
    config_at(tmp_path)
    out, unmatched = tmp_path / "lists", tmp_path / "unmatched.csv"
    args = [
        "--config-path",
        str(tmp_path / "config.toml"),
        "playlist",
        "export",
        "spotify",
        "--all",
        "-q",
    ]
    args += [
        "--format",
        "m3u8",
        "--library",
        str(library),
        "-o",
        str(out),
        "--unmatched",
        str(unmatched),
    ]
    result = CliRunner().invoke(rip, args)
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == [
        "Liked Songs.m3u8",
        "Road trip.m3u8",
    ]
    assert "../Music/Artist B/02. Song B.m4a" in (out / "Road trip.m3u8").read_text(
        encoding="utf-8"
    )
    assert [t.title for t in read_tracks(unmatched)] == ["Gone"]

    result = CliRunner().invoke(rip, [*args[:5], "--liked", "--library", str(library)])
    assert result.exit_code == 2
    assert "go with --format m3u8" in result.output
