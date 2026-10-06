"""Saving the config replaces it in one step: it is never left half written."""

import os

import pytest

from streamrip.config import Config, set_user_defaults


def _config_with_a_new_token(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    config = Config(str(path))
    config.file.deezer.arl = "new-synthetic-cookie"
    config.file.set_modified()
    return path, config


def test_the_new_contents_appear_in_one_step(tmp_path, monkeypatch):
    path, config = _config_with_a_new_token(tmp_path)
    original = path.read_bytes()
    real_replace = os.replace
    seen = []

    def replace(src, dst):
        # Right up to the swap, the config is still the old one, complete.
        seen.append(path.read_bytes() == original)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)

    config.save_file()

    assert seen == [True]
    assert Config(str(path)).file.deezer.arl == "new-synthetic-cookie"
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


@pytest.mark.parametrize("failing_step", ["fsync", "replace"])
def test_a_failed_save_keeps_the_old_config_and_leaves_no_temp_file(
    tmp_path, monkeypatch, failing_step
):
    path, config = _config_with_a_new_token(tmp_path)
    original = path.read_bytes()

    def disk_full(*_):
        raise OSError("No space left on device")

    monkeypatch.setattr(os, failing_step, disk_full)

    with pytest.raises(OSError, match="No space left"):
        config.save_file()

    assert path.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_an_interrupted_save_keeps_the_old_config(tmp_path, monkeypatch):
    # Ctrl-C is a KeyboardInterrupt, which `except Exception` would let through
    # with the temp file left behind.
    path, config = _config_with_a_new_token(tmp_path)
    original = path.read_bytes()

    def interrupt(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupt)

    with pytest.raises(KeyboardInterrupt):
        config.save_file()

    assert path.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_a_symlinked_config_is_written_through(tmp_path):
    # A config kept in a dotfiles repo must stay a link to it.
    repo = tmp_path / "dotfiles"
    repo.mkdir()
    real = repo / "config.toml"
    set_user_defaults(str(real))
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    config = Config(str(link))
    config.file.deezer.arl = "new-synthetic-cookie"
    config.file.set_modified()

    config.save_file()

    assert link.is_symlink()
    assert Config(str(real)).file.deezer.arl == "new-synthetic-cookie"
    assert sorted(p.name for p in repo.iterdir()) == ["config.toml"]
