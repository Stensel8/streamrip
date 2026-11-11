"""Names from a streaming service must never lead out of the downloads folder."""

import os
from pathlib import Path

import pytest

from streamrip.config import Config, set_user_defaults
from streamrip.filepath_utils import (
    clean_filename,
    clean_filepath,
    ensure_inside,
    fit_filename,
    sanitize_filename,
)
from streamrip.media import track as track_module
from streamrip.media.track import album_folder
from streamrip.metadata import AlbumMetadata
from streamrip.metadata.album import AlbumInfo
from streamrip.metadata.covers import Covers

HOSTILE = [
    "..",
    ".",
    "../x",
    "../../etc/passwd",
    "a/../../b",
    "a/./b/../../../c",
    "/etc/passwd",
    "//etc//passwd",
    "///",
    "",
    " .. /x",
    "x/..",
    "../",
    # Backslashes separate folders on every system: a Windows template is
    # written with them, and `..\..` is as much a way out as `../..` is.
    "..\\..\\x",
    "..\\../x",
    "\\\\server\\share",
    "C:\\Windows\\x",
    "..\\",
]


def _album(artist: str) -> AlbumMetadata:
    return AlbumMetadata(
        AlbumInfo("1", 2, "FLAC"), "Title", artist, "1997", [], Covers(), 10
    )


def _config(folder_format: str | None = None, **downloads) -> Config:
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    for name, value in downloads.items():
        setattr(config.session.downloads, name, value)
    if folder_format:
        config.session.filepaths.folder_format = folder_format
    return config


# --- cleaning a path ----------------------------------------------------------


@pytest.mark.parametrize("restrict", [False, True])
@pytest.mark.parametrize("template", HOSTILE)
def test_a_cleaned_path_stays_inside_the_folder_it_is_joined_to(template, restrict):
    cleaned = clean_filepath(template, restrict)

    assert cleaned.parts
    assert not cleaned.is_absolute()
    assert ".." not in cleaned.parts
    assert "." not in cleaned.parts
    ensure_inside(Path("/music"), Path("/music") / cleaned)


@pytest.mark.parametrize(
    ("template", "parts"),
    [
        ("../x/./y//z", ("__", "x", "_", "y", "z")),
        ("/Artist/Title", ("Artist", "Title")),
        ("Artist\\Title", ("Artist", "Title")),
        # Nothing left is not the downloads folder itself.
        ("//", ("Unknown",)),
        ("", ("Unknown",)),
        ("...", ("Unknown",)),  # pathvalidate drops trailing dots, then it's empty
    ],
)
def test_dot_components_become_underscores_and_empty_ones_vanish(template, parts):
    assert clean_filepath(template) == Path(*parts)


def test_long_components_are_still_truncated():
    cleaned = clean_filepath(("é" * 300) + "/" + ("日本語" * 100))

    assert all(len(part.encode()) <= 240 for part in cleaned.parts)


@pytest.mark.parametrize("stem", ["", ".", ".."])
def test_a_file_name_with_no_name_left_still_has_its_extension(stem):
    # A title of only characters a file name can't hold, or of dots: no
    # ".flac" or "..flac".
    assert fit_filename(stem, "flac") == "Unknown.flac"


# --- ensure_inside ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        ("/music/a", True),
        ("/music/a/b", True),
        ("/music/../music/a", True),
        ("/music/Artist ../x", True),
        ("/music", False),  # the root itself is not an album folder
        ("/music/", False),
        ("/", False),
        ("/music/..", False),
        ("/music/a/../../etc", False),
        ("/music2/a", False),  # shares the prefix, is a sibling
        ("/etc/passwd", False),
    ],
)
def test_ensure_inside_accepts_only_what_is_below_the_root(path, inside):
    if inside:
        assert ensure_inside(Path("/music"), Path(path)) == Path(path)
    else:
        with pytest.raises(ValueError, match="not inside the download folder"):
            ensure_inside(Path("/music"), Path(path))


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_ensure_inside_leaves_symlinks_the_user_made_alone(tmp_path):
    # An artist folder on a NAS is a symlink in the downloads folder.
    nas = tmp_path / "nas"
    nas.mkdir()
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist").symlink_to(nas)
    path = root / "Artist" / "Album"

    assert ensure_inside(root, path) == path


# --- album folders ------------------------------------------------------------


@pytest.mark.parametrize("artist", ["..", " .. ", ".", "", "   ", "/", "../.."])
def test_an_artist_called_dots_cannot_move_an_album_out_of_the_downloads_folder(artist):
    folder = album_folder(
        _config("{albumartist}/{title}"), "soundcloud", _album(artist)
    )

    ensure_inside(Path("/music"), folder)
    assert folder.name == "Title"


@pytest.mark.parametrize(
    ("settings", "artist", "expected"),
    [
        # "{albumartist}/{title}" with no artist used to be "/Title".
        ({"folder_format": "{albumartist}/{title}"}, "", "/music/Title"),
        (
            {
                "folder": "/music/downloads",
                "folder_format": "../../{albumartist}/{title}",
            },
            "Björk",
            "/music/downloads/__/__/Björk/Title",
        ),
        (
            {"create_album_folder": False, "folder_format": "{albumartist}/{title}"},
            "Björk",
            "/music",
        ),
        (
            {"create_album_folder": False, "source_subdirectories": True},
            "Björk",
            "/music/Qobuz",
        ),
    ],
    ids=["no artist", "climbing format", "no album folder", "no album folder, source"],
)
def test_album_folder(settings, artist, expected):
    assert album_folder(_config(**settings), "qobuz", _album(artist)) == Path(expected)


def test_album_folder_refuses_a_path_outside_the_downloads_folder(monkeypatch):
    # The last line of defence, whatever the cleaning above lets through.
    monkeypatch.setattr(track_module, "clean_filepath", lambda *_: Path("../escaped"))

    with pytest.raises(ValueError, match="not inside the download folder"):
        album_folder(_config(), "qobuz", _album("Björk"))


def test_create_album_folder_off_works_under_the_real_default_music_folder(tmp_path):
    # The two don't interact in the code, but a fresh config's actual default
    # (the user's real Music folder, not a stand-in) deserves an end-to-end check.
    config_path = tmp_path / "config.toml"
    set_user_defaults(config_path)
    config = Config(config_path)
    config.session.downloads.create_album_folder = False

    folder = album_folder(config, "qobuz", _album("Björk"))

    assert folder == Path(config.session.downloads.folder)


# --- file names ---------------------------------------------------------------


# What pathvalidate 3.3.1's sanitize_filename made of each, which streamrip used
# before its own: the names of an existing library must not change.
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", ""),
        (" ", ""),
        (".", "."),
        ("..", ".."),
        ("...", ""),
        (". .", ""),
        (" a. ", "a"),
        ("a..", "a"),
        ("...a", "...a"),
        (".a", ".a"),
        ("CON", "CON_"),
        ("con.txt", "con_.txt"),
        ("CON.CON", "CON_.CON_"),
        ("CON ", "CON"),
        (" CON", "CON"),
        ("con.", "con_."),
        (".con", ".con"),
        ("x.CON", "x.CON"),
        ("AUX.tar.gz", "AUX_.tar.gz"),
        ("COM1.flac", "COM1_.flac"),
        ("LPT³", "LPT³_"),
        ("CLOCK$", "CLOCK$_"),
        ("a/b", "ab"),
        ("a\\b", "ab"),
        ("C:foo", "Cfoo"),
        ('AC/DC: "Who?" <live> | *', "ACDC Who live"),
        ("tab\there\nnewline", "tabherenewline"),
        ("\x00\x1f\x7fok", "ok"),
        ("\xa0", ""),
        ("\u3000x\u3000", "\u3000x\u3000"),
        (":\xa0", ""),
        ("Björk \u2013 Jóga", "Björk \u2013 Jóga"),
        ("日本語のタイトル", "日本語のタイトル"),
        ("trail. ", "trail"),
        # Cut at 255 bytes, without splitting a character.
        ("a" * 260, "a" * 255),
        ("é" * 130, "é" * 127),  # 254 bytes
    ],
)
def test_sanitize_filename_names_files_as_pathvalidate_did(name, expected):
    assert sanitize_filename(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CON★", "CON_"),  # not the Windows device name
        ("Love ♥", "Love"),  # no trailing space, which Windows drops
        ("★ Japanese", "Japanese"),
    ],
)
def test_restricted_names_are_sanitized_after_the_filtering(name, expected):
    assert clean_filename(name, restrict=True) == expected
