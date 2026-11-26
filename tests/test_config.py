import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from streamrip import config as config_module
from streamrip.accounts import Accounts
from streamrip.config import (
    BLANK_CONFIG_PATH,
    Config,
    ConfigData,
    set_user_defaults,
)

SAMPLE_CONFIG = Path("tests/test_config.toml")


@pytest.fixture
def config_path(tmp_path) -> Path:
    """A config made from the template, as on a first run."""
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    return path


def _new_token(path: Path) -> Config:
    """The config at `path`, with a changed token that is not saved yet."""
    config = Config(path)
    config.file.deezer.arl = "new-synthetic-cookie"
    config.file.set_modified()
    return config


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_sample_config_loads():
    loaded = ConfigData.from_toml(SAMPLE_CONFIG.read_text(encoding="utf-8"))
    assert loaded.downloads.folder == "test_folder"
    assert loaded.qobuz.secrets == ["secret1", "secret2"]
    assert (loaded.tidal.quality, loaded.deezer.arl) == (3, "testarl")


def test_a_config_saved_with_a_byte_order_mark_loads(tmp_path):
    # Notepad's "UTF-8 with BOM", and PowerShell 5.1's Set-Content -Encoding UTF8.
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf" + SAMPLE_CONFIG.read_bytes())
    assert Config(path).session.downloads.folder == "test_folder"


def test_the_template_and_the_config_classes_agree():
    """A field the template lacks fails loading; a key the classes lack (one an
    earlier version wrote, like 2.8.0-rc1's credentials) is left out, and named."""
    template = BLANK_CONFIG_PATH.read_text()
    assert ConfigData.from_toml(template).unused == ()

    old = template.replace("[spotify]\n", '[spotify]\ncredentials = ""\n', 1)
    assert ConfigData.from_toml(old).unused == ("[spotify] credentials",)
    with pytest.raises(TypeError, match="max_search_results"):
        ConfigData.from_toml(
            re.sub(r"^max_search_results = .*\n", "", template, flags=re.MULTILINE)
        )


def test_user_defaults_download_to_the_music_folder(tmp_path, monkeypatch):
    fake_music = tmp_path / "home" / "Music"
    monkeypatch.setattr(config_module, "DEFAULT_DOWNLOADS_FOLDER", fake_music)
    path = tmp_path / "config.toml"

    set_user_defaults(path)

    assert Config(path).session.downloads.folder == str(fake_music)

    # Empty, as in the template, is the same place: not the folder streamrip
    # is run in, nor a database gone after the run.
    path.write_text(BLANK_CONFIG_PATH.read_text())
    session = Config(path).session
    assert session.downloads.folder == str(fake_music)
    assert session.database.downloads_path == str(tmp_path / "downloads.db")


def test_the_music_folder_follows_the_users_language_on_linux(monkeypatch):
    done = subprocess.CompletedProcess([], 0, stdout="/data/Muziek\n")
    run = MagicMock(return_value=done)
    monkeypatch.setattr(config_module.subprocess, "run", run)
    assert config_module._xdg_music_dir() == Path("/data/Muziek")

    run.side_effect = FileNotFoundError("no xdg-user-dir")
    assert config_module._xdg_music_dir() is None


# --- saving -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("modified", "folder"), [(True, "new"), (False, "test_folder")]
)
def test_only_a_modified_config_is_saved(tmp_path, modified, folder):
    path = tmp_path / "config.toml"
    shutil.copy(SAMPLE_CONFIG, path)
    config = Config(path)
    config.file.downloads.folder = "new"
    if modified:
        config.file.set_modified()
    config.save_file()

    assert Config(path).session.downloads.folder == folder


def test_a_tilde_means_the_home_folder_and_stays_in_the_file(tmp_path):
    path = tmp_path / "config.toml"
    shutil.copy(SAMPLE_CONFIG, path)
    path.write_text(path.read_text().replace('"test_folder"', '"~/Music"', 1))
    config = Config(path)

    assert config.session.downloads.folder == str(Path.home() / "Music")
    assert config.file.downloads.folder == "~/Music"


def test_saving_keeps_what_was_changed_in_the_file_meanwhile(config_path):
    # A run saves when it ends, which can be hours after it read the config.
    # Written back whole, it undid an edit made in between.
    config = _new_token(config_path)  # this run: a new Deezer login
    other = Config(config_path)  # meanwhile: an edit by hand, another run's token
    other.file.downloads.max_connections = 2
    other.file.tidal.access_token = "the-other-run's"
    other.file.set_modified()
    other.save_file()

    config.save_file()

    saved = Config(config_path).file
    assert saved.deezer.arl == "new-synthetic-cookie"
    assert saved.downloads.max_connections == 2
    assert saved.tidal.access_token == "the-other-run's"


def test_a_server_takes_on_what_another_program_wrote(config_path):
    server = Config(config_path)
    server.file.cli.max_search_results = 7
    server.file.set_modified()
    server.save_file()
    assert not server.reload_if_changed()  # its own saving is no news

    command = Config(config_path)  # `streamrip login` saves a login meanwhile
    command.update_logins("deezer", {"arl": "from-the-command-line"})

    assert server.reload_if_changed()
    assert server.session.deezer.arl == "from-the-command-line"
    assert server.session.cli.max_search_results == 7


def test_saving_leaves_a_commented_option_commented(config_path):
    # Written beside the commented line, it would make uncommenting that a
    # duplicate key, and the config would not load.
    _new_token(config_path).save_file()

    saved = config_path.read_text()
    assert "\n# no_update_check = true\n" in saved
    assert "\nno_update_check" not in saved
    config_path.write_text(
        saved.replace("# no_update_check = true", "no_update_check = true")
    )
    assert Config(config_path).session.cli.no_update_check is True


@pytest.mark.parametrize(
    ("failing_step", "error"),
    [
        ("fsync", OSError("No space left on device")),
        # Ctrl-C, which `except Exception` would let through with the temp file left.
        ("replace", KeyboardInterrupt()),
    ],
)
def test_a_failed_save_keeps_the_old_config_and_leaves_no_temp_file(
    config_path, monkeypatch, failing_step, error
):
    config = _new_token(config_path)
    original = config_path.read_bytes()

    def fail(*_):
        raise error

    monkeypatch.setattr(os, failing_step, fail)

    with pytest.raises(type(error)):
        config.save_file()

    assert config_path.read_bytes() == original
    assert [p.name for p in config_path.parent.iterdir()] == ["config.toml"]


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_a_symlinked_config_is_written_through(tmp_path):
    # A config kept in a dotfiles repo must stay a link to it.
    repo = tmp_path / "dotfiles"
    repo.mkdir()
    real = repo / "config.toml"
    set_user_defaults(real)
    link = tmp_path / "config.toml"
    link.symlink_to(real)

    _new_token(link).save_file()

    assert link.is_symlink()
    assert Config(link).file.deezer.arl == "new-synthetic-cookie"
    assert sorted(p.name for p in repo.iterdir()) == ["config.toml"]


def test_a_config_can_be_made_in_a_folder_that_is_not_there_yet(tmp_path):
    path = tmp_path / "new" / "folder" / "config.toml"
    set_user_defaults(path)
    assert path.is_file()


# --- credentials must stay private whatever the umask ------------------------


@pytest.fixture
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
@pytest.mark.usefixtures("permissive_umask")
class TestPrivateFiles:
    @pytest.mark.parametrize("existing_mode", [None, 0o644])
    def test_creation_and_reset_are_private(self, tmp_path, existing_mode):
        path = tmp_path / "config.toml"
        if existing_mode is not None:
            path.write_text("old contents")
            path.chmod(existing_mode)

        set_user_defaults(path)

        assert mode(path) == 0o600
        assert Config(path).file.deezer.arl == ""

    @pytest.mark.login
    @pytest.mark.parametrize("name", ["config.toml", "state.toml"])
    def test_loading_existing_credentials_repairs_permissions(self, tmp_path, name):
        # A folder copied with `cp -r`: state.toml has the logins themselves.
        path = tmp_path / "config.toml"
        path.write_text(BLANK_CONFIG_PATH.read_text())
        (tmp_path / "state.toml").write_text('[deezer]\narl = "cookie"\n')
        (tmp_path / name).chmod(0o644)

        Config(path).save_file()  # Nothing was marked modified.

        assert mode(tmp_path / name) == 0o600

    @pytest.mark.login
    @pytest.mark.parametrize("remove_file", [False, True])
    def test_saving_credentials_secures_existing_or_recreated_file(
        self, config_path, remove_file
    ):
        config = _new_token(config_path)
        if remove_file:
            config_path.unlink()
        else:
            config_path.chmod(0o666)  # Permissions can change after loading.

        config.save_file()

        assert mode(config_path) == 0o600
        assert Config(config_path).file.deezer.arl == "new-synthetic-cookie"

    @pytest.mark.parametrize("already_exists", [False, True])
    def test_app_directory_is_private(self, tmp_path, already_exists):
        app_dir = tmp_path / "streamrip"
        if already_exists:
            app_dir.mkdir(mode=0o755)
        # Override Click's directory lookup, so the user's own folder is left alone.
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import click, sys; click.get_app_dir = lambda _: sys.argv[1]; "
                    "import streamrip.config"
                ),
                str(app_dir),
            ],
            check=True,
        )
        assert mode(app_dir) == 0o700

    @pytest.mark.login
    def test_a_config_that_cannot_be_made_private_still_loads(
        self, config_path, monkeypatch, caplog
    ):
        # Another user's file, or one on a FAT stick: chmod fails there.
        config_path.chmod(0o644)

        def refused(*_):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "fchmod", refused)

        assert Config(config_path).file.deezer.arl == ""
        assert "Others can read" in caplog.text


# --- logins live in state.toml ------------------------------------------------


def test_logins_are_saved_in_state_toml_and_leave_config_toml(config_path):
    with Config(config_path) as config:
        config.file.deezer.arl = "cookie"
        config.file.deezer.quality = 1
        config.file.set_modified()

    state = tomllib.loads((config_path.parent / "state.toml").read_text())
    assert state == {"deezer": {"arl": "cookie"}}
    saved = tomllib.loads(config_path.read_text())
    assert "arl" not in saved["deezer"]
    assert saved["deezer"]["quality"] == 1
    assert Config(config_path).session.deezer.arl == "cookie"


def test_a_config_from_before_state_toml_moves_its_logins_there(config_path):
    # Until 2.8 the logins were in config.toml. Left there, a logout (saved in
    # state.toml) brought them back; a key state.toml has empty must not wipe them.
    config_path.write_text(
        config_path.read_text()
        .replace("[deezer]\n", '[deezer]\narl = "old-cookie"\n', 1)
        .replace("[qobuz]\n", '[qobuz]\nuser_id = "42"\nauth_token = "old"\n', 1)
    )
    (config_path.parent / "state.toml").write_text('[deezer]\narl = ""\n')

    assert Accounts(Config(config_path)).logged_in("deezer")
    saved = tomllib.loads(config_path.read_text())
    assert "arl" not in saved["deezer"]
    assert "auth_token" not in saved["qobuz"]

    Accounts(Config(config_path)).logout("deezer")
    accounts = Accounts(Config(config_path))
    assert (accounts.logged_in("deezer"), accounts.logged_in("qobuz")) == (False, True)


def test_two_programs_saving_their_own_logins_keep_each_others(config_path):
    web, terminal = Config(config_path), Config(config_path)
    web.file.spotify.access_token = "spotify-token"
    terminal.file.deezer.arl = "deezer-cookie"
    web.save_file()
    terminal.save_file()

    saved = Config(config_path).file
    assert (saved.spotify.access_token, saved.deezer.arl) == (
        "spotify-token",
        "deezer-cookie",
    )


def test_a_logout_is_saved(config_path):
    # The web interface saves after every login; a logout sets the token back to
    # what it was at loading, which the next save then left out.
    with Config(config_path) as config:
        config.file.deezer.arl = "cookie"
    with Config(config_path) as config:
        config.file.deezer.arl = ""
    assert Config(config_path).session.deezer.arl == ""


def test_a_login_another_program_saved_is_taken_on_before_a_token_is_refreshed(
    config_path,
):
    web, terminal = Config(config_path), Config(config_path)
    terminal.update_logins(
        "spotify", {"access_token": "new", "refresh_token": "rotated"}
    )

    web.session.spotify.client_id = "not saved yet"  # a change of this program's own
    web.reload_logins("spotify")

    assert web.session.spotify.refresh_token == "rotated"
    assert web.file.spotify.refresh_token == "rotated"
    assert web.session.spotify.client_id == "not saved yet"  # left alone
