"""Playlists as M3U8 files that point at the music of a library."""

import shutil
from pathlib import Path

import mutagen
import pytest
from click.testing import CliRunner
from playlist_support import (
    ISRC_A,
    ISRC_B,
    FakeProvider,
    FakeServices,
    config_at,
    track,
)

from streamrip.rip import playlist_cli
from streamrip.rip.cli import rip
from streamrip.sync.csvio import read_tracks
from streamrip.sync.m3u import Library, read_file, write_m3u8
from streamrip.sync.models import Track

HERE = Path(__file__).parent


def tagged(folder: Path, name: str, source: str, **tags) -> Path:
    """A copy of a silent test file, with these (easy) tags."""
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
    flac, m4a = "silence.flac", "silence.m4a"
    song_a = {"title": "Song A", "artist": "Artist A", "isrc": ISRC_A}
    # One recording on two releases; the file names say nothing, the ISRC finds it.
    tagged(music, "Various/Hits/07. Song A.flac", flac, **song_a, date="2020")
    tagged(
        music, "Artist A/Album/01. sng a (Explicit).flac", flac, **song_a, date="2001"
    )
    # No ISRC: found by title and artist.
    tagged(music, "Artist B/02. Song B.m4a", m4a, title="Song B", artist="Artist B")
    (music / "cover.jpg").write_bytes(b"not audio")
    return music


def test_tags_are_read_from_flac_and_m4a(tmp_path):
    flac = tagged(tmp_path, "a.flac", "silence.flac", title="T", isrc="us-aaa-00-00001")
    m4a = tagged(tmp_path, "b.m4a", "silence.m4a", title="U", isrc=ISRC_B)
    assert read_file(flac).isrc == ISRC_A  # in its canonical form
    assert (read_file(m4a).title, read_file(m4a).isrc) == ("U", ISRC_B)


def test_a_playlist_points_at_the_files_of_the_library(tmp_path, library):
    music = Library.scan(library)
    assert music.size == 3  # the cover is not music
    wanted = [
        track("Song A", "Artist A", isrc=ISRC_A),
        track("Song B", "Artist B", duration_ms=None),  # the test files are silent
        track("Not here", "Nobody"),
    ]
    playlist = tmp_path / "Music" / "Playlists" / "Mix.m3u8"
    written, missing = write_m3u8(playlist, wanted, music)
    assert (written, [t.title for t in missing]) == (2, ["Not here"])
    lines = playlist.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1].endswith(",Artist A - Song A")
    # Relative to the playlist, so the library can be mounted elsewhere.
    assert lines[2] == "../Artist A/Album/01. sng a (Explicit).flac"
    assert lines[4] == "../Artist B/02. Song B.m4a"

    write_m3u8(playlist, wanted[:1], music, absolute=True)
    assert playlist.read_text(encoding="utf-8").splitlines()[2] == str(
        library / "Artist A/Album/01. sng a (Explicit).flac"
    )


def test_a_file_is_found_however_its_tag_joins_the_artists(tmp_path):
    # Picard writes "A feat. B" where the service lists A and B.
    artist = "Calvin Harris feat. Rihanna"
    tagged(tmp_path, "a.flac", "silence.flac", title="Song", artist=artist)
    wanted = Track("Song", ["Calvin Harris", "Rihanna"])
    assert Library.scan(tmp_path).find(wanted) is not None


def test_a_linked_file_keeps_its_path_in_the_library(tmp_path):
    # A player may see only the library, not the disk a link in it leads to.
    real = tagged(tmp_path / "disk", "a.flac", "silence.flac", title="A", isrc=ISRC_A)
    tagged(
        tmp_path / "nas" / "Artist", "b.flac", "silence.flac", title="B", isrc=ISRC_B
    )
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.flac").symlink_to(real)
    (music / "Artist").symlink_to(tmp_path / "nas" / "Artist")  # a linked folder
    # The playlists through a link to the library, the library by its real path.
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "Music").symlink_to(music)
    playlist = tmp_path / "home" / "Music" / "Playlists" / "x.m3u8"
    wanted = [track("A", isrc=ISRC_A), track("B", isrc=ISRC_B)]
    write_m3u8(playlist, wanted, Library.scan(music))
    assert playlist.read_text(encoding="utf-8").splitlines()[2::2] == [
        "../a.flac",
        "../Artist/b.flac",
    ]


def test_without_a_library_the_playlist_only_describes_the_tracks(tmp_path):
    path = tmp_path / "x.m3u8"
    wanted = [track("Song A", "Artist A", album="Album", duration_ms=200_000)]
    assert write_m3u8(path, wanted) == (1, [])
    assert (
        path.read_text(encoding="utf-8")
        == "#EXTM3U\n#EXTINF:200,Artist A - Song A\n#EXTALB:Album\n"
    )


def test_export_writes_m3u8_playlists_and_what_the_library_lacks(
    tmp_path, library, monkeypatch
):
    spotify = FakeProvider(
        "spotify",
        liked=[track("Song A", "Artist A", isrc=ISRC_A), track("Gone", "Nobody")],
    )
    spotify.existing_playlist(
        "Road trip", track("Song B", "Artist B", duration_ms=None)
    )
    monkeypatch.setattr(
        playlist_cli, "_services", lambda _ctx: FakeServices(tmp_path, spotify=spotify)
    )
    monkeypatch.setenv("STREAMRIP_NO_UPDATE_CHECK", "1")
    config_at(tmp_path)
    out, unmatched = tmp_path / "lists", tmp_path / "unmatched.csv"
    args = ["--config-path", str(tmp_path / "config.toml")]
    args += ["playlist", "export", "spotify", "--all", "-q", "--format", "m3u8"]
    args += ["--library", str(library), "-o", str(out), "--unmatched", str(unmatched)]

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
