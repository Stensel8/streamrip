"""Names from a streaming service must never lead out of the downloads folder."""

import os

import pytest

from streamrip.config import Config
from streamrip.filepath_utils import clean_filepath, ensure_inside
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

    assert cleaned
    assert not os.path.isabs(cleaned)
    assert ".." not in cleaned.split(os.sep)
    assert "." not in cleaned.split(os.sep)
    ensure_inside("/music", os.path.join("/music", cleaned))


@pytest.mark.parametrize("restrict", [False, True])
@pytest.mark.parametrize("template", HOSTILE)
def test_restricting_characters_changes_nothing_about_that(template, restrict):
    cleaned = clean_filepath(template, restrict)

    ensure_inside("/music", os.path.join("/music", cleaned))


def test_dot_components_become_underscores_and_empty_ones_vanish():
    assert clean_filepath("../x/./y//z") == os.sep.join(["__", "x", "_", "y", "z"])
    assert clean_filepath("/Artist/Title") == os.sep.join(["Artist", "Title"])


def test_a_path_with_nothing_left_is_not_the_downloads_folder_itself():
    assert clean_filepath("//") == "Unknown"
    assert clean_filepath("") == "Unknown"


def test_a_dots_only_name_has_nothing_left_to_be_a_folder():
    # pathvalidate drops trailing dots, so "..." is empty, which is dropped.
    assert clean_filepath("...") == "Unknown"


@pytest.mark.parametrize(
    "template",
    ["..\\..\\x", "..\\../x", "\\\\server\\share", "C:\\Windows\\x", "..\\"],
)
def test_backslashes_separate_folders_on_every_system(template):
    # A Windows template is written with them, and `..\\..` is as much a way
    # out as `../..` is.
    cleaned = clean_filepath(template)

    assert not os.path.isabs(cleaned)
    assert ".." not in cleaned.split(os.sep)
    ensure_inside("/music", os.path.join("/music", cleaned))


def test_a_backslash_template_is_the_same_as_a_slash_template():
    assert clean_filepath("Artist\\Title") == clean_filepath("Artist/Title")


def test_long_components_are_still_truncated():
    cleaned = clean_filepath(("é" * 300) + "/" + ("日本語" * 100))

    assert all(len(part.encode()) <= 240 for part in cleaned.split(os.sep))


@pytest.mark.parametrize(
    "path",
    ["/music/a", "/music/a/b", "/music/../music/a", "/music/Artist ../x"],
)
def test_ensure_inside_accepts_what_is_below_the_root(path):
    assert ensure_inside("/music", path) == path


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
        ensure_inside("/music", path)


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_ensure_inside_leaves_symlinks_the_user_made_alone(tmp_path):
    # An artist folder on a NAS is a symlink in the downloads folder.
    nas = tmp_path / "nas"
    nas.mkdir()
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist").symlink_to(nas)
    path = str(root / "Artist" / "Album")

    assert ensure_inside(str(root), path) == path


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

    ensure_inside("/music", folder)
    assert os.path.basename(folder) == "Title"


def test_an_empty_artist_does_not_turn_the_folder_into_an_absolute_path():
    # "{albumartist}/{title}" with no artist used to be "/Title".
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.filepaths.folder_format = "{albumartist}/{title}"

    assert album_folder(config, "qobuz", _album("")) == "/music/Title"


def test_a_folder_format_cannot_climb_out_either():
    config = Config.defaults()
    config.session.downloads.folder = "/music/downloads"
    config.session.filepaths.folder_format = "../../{albumartist}/{title}"

    folder = album_folder(config, "qobuz", _album("Björk"))

    assert folder == os.path.join("/music/downloads", "__", "__", "Björk", "Title")


def test_album_folder_refuses_a_path_outside_the_downloads_folder(monkeypatch):
    # The last line of defence, whatever the cleaning above lets through.
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    monkeypatch.setattr(track_module, "clean_filepath", lambda *_: "../escaped")

    with pytest.raises(ValueError, match="not inside the download folder"):
        album_folder(config, "qobuz", _album("Björk"))
