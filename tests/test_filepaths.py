"""Names from a streaming service must never lead out of the downloads folder."""

import os
from pathlib import Path

import pytest

from streamrip.config import Config
from streamrip.filepath_utils import (
    clean_filename,
    clean_filepath,
    ensure_inside,
    fit_filename,
    sanitize_filename,
)
from streamrip.media.track import album_folder
from streamrip.metadata import AlbumMetadata
from streamrip.metadata.album import AlbumInfo
from streamrip.metadata.covers import Covers


def _album(artist: str, title: str = "Title") -> AlbumMetadata:
    return AlbumMetadata(
        AlbumInfo("1", 2, "FLAC"), title, artist, "1997", [], Covers(), 10
    )


def _config(**downloads) -> Config:
    config = Config.defaults()
    config.session.downloads.folder = "/music"
    config.session.filepaths.folder_format = "{albumartist}/{title}"
    for name, value in downloads.items():
        setattr(config.session.downloads, name, value)
    return config


@pytest.mark.parametrize(
    ("template", "parts"),
    [
        ("../x/./y//z", ("__", "x", "_", "y", "z")),
        ("../../etc/passwd", ("__", "__", "etc", "passwd")),
        ("/Artist/Title", ("Artist", "Title")),
        ("Artist\\Title", ("Artist", "Title")),  # a Windows template
        ("", ("Unknown",)),
    ],
)
def test_dots_and_slashes_in_a_path_cannot_climb_out(template, parts):
    assert clean_filepath(template) == Path(*parts)


def test_long_components_are_truncated():
    cleaned = clean_filepath(("é" * 300) + "/" + ("日本語" * 100))

    assert all(len(part.encode()) <= 240 for part in cleaned.parts)


@pytest.mark.parametrize("stem", ["", ".."])
def test_a_file_name_with_no_name_left_keeps_its_extension(stem):
    assert fit_filename(stem, "flac") == "Unknown.flac"


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        ("/music/Artist/Album", True),
        ("/music", False),  # the root itself is not an album folder
        ("/music/a/../../etc", False),
        ("/music2/a", False),  # shares the prefix, is a sibling
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


@pytest.mark.parametrize("artist", ["..", " .. ", "", "../.."])
def test_an_artist_called_dots_cannot_move_an_album_out(artist):
    # SoundCloud names are free text.
    folder = album_folder(_config(), "soundcloud", _album(artist))

    ensure_inside(Path("/music"), folder)
    assert folder.name == "Title"


@pytest.mark.parametrize(
    ("settings", "artist", "expected"),
    [
        # "{albumartist}/{title}" with no artist used to be "/Title".
        ({}, "", "/music/Title"),
        ({}, "Björk", "/music/Björk/Title"),
        ({"create_album_folder": False}, "Björk", "/music"),
        (
            {"create_album_folder": False, "source_subdirectories": True},
            "Björk",
            "/music/Qobuz",
        ),
    ],
)
def test_album_folder(settings, artist, expected):
    assert album_folder(_config(**settings), "qobuz", _album(artist)) == Path(expected)


@pytest.mark.parametrize(
    ("restrict", "folder"),
    [(False, "/music/Björk - Homogénic"), (True, "/music/Bjrk - Homognic")],
)
def test_album_folder_follows_restrict_characters(restrict, folder):
    # Singles built their album folder without it, so with it on they landed
    # beside their album instead of in it.
    config = _config()
    config.session.filepaths.folder_format = "{albumartist} - {title}"
    config.session.filepaths.restrict_characters = restrict

    assert album_folder(config, "qobuz", _album("Björk", "Homogénic")) == Path(folder)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ('AC/DC: "Who?" <live> | *', "ACDC Who live"),
        ("tab\there\nnewline", "tabherenewline"),
        ("trail. ", "trail"),
        ("con.txt", "con_.txt"),  # a Windows device name
        ("Björk Jóga", "Björk Jóga"),
        ("日本語のタイトル", "日本語のタイトル"),
        ("a" * 260, "a" * 255),  # cut at 255 bytes
        ("é" * 130, "é" * 127),  # without splitting a character
    ],
)
def test_sanitize_filename(name, expected):
    # The same names the pathvalidate streamrip used before made: an existing
    # library must not change.
    assert sanitize_filename(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("CON★", "CON_"), ("Love ♥", "Love")],  # no trailing space, which Windows drops
)
def test_restricted_names_are_sanitized_after_the_filtering(name, expected):
    assert clean_filename(name, restrict=True) == expected
