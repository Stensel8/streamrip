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
]


@pytest.mark.parametrize("template", HOSTILE)
def test_a_cleaned_path_stays_inside_the_folder_it_is_joined_to(template):
    cleaned = clean_filepath(template)

    assert cleaned.parts
    assert not cleaned.is_absolute()
    assert ".." not in cleaned.parts
    assert "." not in cleaned.parts
    ensure_inside(Path("/music"), Path("/music") / cleaned)


@pytest.mark.parametrize("restrict", [False, True])
@pytest.mark.parametrize("template", HOSTILE)
def test_restricting_characters_changes_nothing_about_that(template, restrict):
    cleaned = clean_filepath(template, restrict)

    ensure_inside(Path("/music"), Path("/music") / cleaned)


def test_dot_components_become_underscores_and_empty_ones_vanish():
    assert clean_filepath("../x/./y//z") == Path("__", "x", "_", "y", "z")
    assert clean_filepath("/Artist/Title") == Path("Artist", "Title")


def test_a_path_with_nothing_left_is_not_the_downloads_folder_itself():
    assert clean_filepath("//") == Path("Unknown")
    assert clean_filepath("") == Path("Unknown")


def test_a_dots_only_name_has_nothing_left_to_be_a_folder():
    # pathvalidate drops trailing dots, so "..." is empty, which is dropped.
    assert clean_filepath("...") == Path("Unknown")


@pytest.mark.parametrize("stem", ["", ".", ".."])
def test_a_file_name_with_no_name_left_still_has_its_extension(stem):
    """A title of only characters a file name can't hold, or of dots, leaves
    no ".flac" or "..flac"."""
    assert fit_filename(stem, "flac") == "Unknown.flac"


@pytest.mark.parametrize(
    "template",
    ["..\\..\\x", "..\\../x", "\\\\server\\share", "C:\\Windows\\x", "..\\"],
)
def test_backslashes_separate_folders_on_every_system(template):
    # A Windows template is written with them, and `..\\..` is as much a way
    # out as `../..` is.
    cleaned = clean_filepath(template)

    assert not cleaned.is_absolute()
    assert ".." not in cleaned.parts
    ensure_inside(Path("/music"), Path("/music") / cleaned)


def test_a_backslash_template_is_the_same_as_a_slash_template():
    assert clean_filepath("Artist\\Title") == clean_filepath("Artist/Title")


def test_long_components_are_still_truncated():
    cleaned = clean_filepath(("é" * 300) + "/" + ("日本語" * 100))

    assert all(len(part.encode()) <= 240 for part in cleaned.parts)


@pytest.mark.parametrize(
    "path",
    ["/music/a", "/music/a/b", "/music/../music/a", "/music/Artist ../x"],
)
def test_ensure_inside_accepts_what_is_below_the_root(path):
    assert ensure_inside(Path("/music"), Path(path)) == Path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/music",  # the root itself is not an album folder
        "/music/",
        "/",
        "/music/..",
        "/music/a/../../etc",
        "/music2/a",  # shares the prefix, is a sibling
        "/etc/passwd",
    ],
)
def test_ensure_inside_rejects_everything_else(path):
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


def _album(artist: str, title: str = "Title") -> AlbumMetadata:
    return AlbumMetadata(
        AlbumInfo("1", 2, "FLAC"), title, artist, "1997", [], Covers(), 10
    )


@pytest.mark.parametrize("artist", ["..", " .. ", ".", "", "   ", "/", "../.."])
def test_an_artist_called_dots_cannot_move_an_album_out_of_the_downloads_folder(artist):
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.filepaths.folder_format = "{albumartist}/{title}"

    folder = album_folder(config, "soundcloud", _album(artist))

    ensure_inside(Path("/music"), folder)
    assert folder.name == "Title"


def test_an_empty_artist_does_not_turn_the_folder_into_an_absolute_path():
    # "{albumartist}/{title}" with no artist used to be "/Title".
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.filepaths.folder_format = "{albumartist}/{title}"

    assert album_folder(config, "qobuz", _album("")) == Path("/music/Title")


def test_a_folder_format_cannot_climb_out_either():
    config = Config.defaults()
    config.session.downloads.folder = "/music/downloads"
    config.session.filepaths.folder_format = "../../{albumartist}/{title}"

    folder = album_folder(config, "qobuz", _album("Björk"))

    assert folder == Path("/music/downloads/__/__/Björk/Title")


def test_album_folder_refuses_a_path_outside_the_downloads_folder(monkeypatch):
    # The last line of defence, whatever the cleaning above lets through.
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    monkeypatch.setattr(track_module, "clean_filepath", lambda *_: Path("../escaped"))

    with pytest.raises(ValueError, match="not inside the download folder"):
        album_folder(config, "qobuz", _album("Björk"))


def test_create_album_folder_off_puts_every_album_in_the_downloads_folder():
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.downloads.create_album_folder = False
    config.session.filepaths.folder_format = "{albumartist}/{title}"

    assert album_folder(config, "qobuz", _album("Björk")) == Path("/music")
    assert album_folder(config, "qobuz", _album("Adele", "25")) == Path("/music")


def test_create_album_folder_off_still_separates_by_source():
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.downloads.create_album_folder = False
    config.session.downloads.source_subdirectories = True

    assert album_folder(config, "qobuz", _album("Björk")) == Path("/music/Qobuz")


def test_create_album_folder_off_works_under_the_real_default_music_folder(tmp_path):
    # The two don't interact in the code -- create_album_folder only ever
    # reads whatever string is in downloads.folder -- but a fresh config's
    # actual default (the user's real Music folder, not a hardcoded stand-in)
    # deserves its own end-to-end check, not just an assumption.
    config_path = tmp_path / "config.toml"
    set_user_defaults(config_path)
    config = Config(config_path)
    config.session.downloads.create_album_folder = False

    folder = album_folder(config, "qobuz", _album("Björk"))

    assert folder == Path(config.session.downloads.folder)


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
    ],
)
def test_sanitize_filename_names_files_as_pathvalidate_did(name, expected):
    assert sanitize_filename(name) == expected


def test_sanitize_filename_cuts_at_255_bytes_without_splitting_a_character():
    assert sanitize_filename("a" * 260) == "a" * 255
    assert sanitize_filename("é" * 130) == "é" * 127  # 254 bytes


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
