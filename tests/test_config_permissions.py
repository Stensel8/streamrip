"""Credential files must be private even when the caller's umask is permissive."""

import os
import stat
import subprocess
import sys

import pytest

from streamrip.config import BLANK_CONFIG_PATH, Config, set_user_defaults

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")


@pytest.fixture(autouse=True)
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("existing_mode", [None, 0o600, 0o644, 0o666])
def test_creation_and_reset_are_private(tmp_path, existing_mode):
    path = tmp_path / "config.toml"
    if existing_mode is not None:
        path.write_text("old contents" * 1000)
        path.chmod(existing_mode)
    parent_mode = mode(tmp_path)

    set_user_defaults(str(path))

    assert mode(path) == 0o600
    assert mode(tmp_path) == parent_mode
    assert Config(str(path)).file.deezer.arl == ""


def test_loading_existing_credentials_repairs_permissions(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(open(BLANK_CONFIG_PATH).read())
    path.chmod(0o644)
    original = path.read_bytes()

    config = Config(str(path))
    config.save_file()  # Nothing was marked modified.

    assert mode(path) == 0o600
    assert path.read_bytes() == original


@pytest.mark.parametrize("remove_file", [False, True])
def test_saving_credentials_secures_existing_or_recreated_file(tmp_path, remove_file):
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    config = Config(str(path))
    config.file.qobuz.auth_token = "synthetic-qobuz-token"
    config.file.deezer.arl = "synthetic-deezer-cookie"
    config.file.tidal.refresh_token = "synthetic-tidal-token"
    config.file.set_modified()
    if remove_file:
        path.unlink()
    else:
        path.chmod(0o666)  # Permissions can change after loading the config.

    config.save_file()

    assert mode(path) == 0o600
    saved = Config(str(path)).file
    assert saved.qobuz.auth_token == "synthetic-qobuz-token"
    assert saved.deezer.arl == "synthetic-deezer-cookie"
    assert saved.tidal.refresh_token == "synthetic-tidal-token"


@pytest.mark.parametrize("operation", ["save", "reset"])
def test_permission_failure_aborts_before_changing_contents(
    tmp_path, monkeypatch, operation
):
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    config = Config(str(path))
    config.file.deezer.arl = "new-synthetic-cookie"
    config.file.set_modified()
    path.chmod(0o644)
    original = path.read_bytes()
    descriptors = []

    def deny_chmod(fd, permissions):
        descriptors.append(fd)
        raise PermissionError("Cannot restrict permissions")

    monkeypatch.setattr(os, "fchmod", deny_chmod)
    with pytest.raises(PermissionError):
        if operation == "save":
            config.save_file()
        else:
            set_user_defaults(str(path))

    assert path.read_bytes() == original
    assert descriptors
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)  # Failed saves must also close their file descriptor.


def test_loading_defaults_does_not_chmod_packaged_template(monkeypatch):
    def unexpected_chmod(*args):
        pytest.fail("Loading defaults must not chmod the packaged template")

    monkeypatch.setattr(os, "fchmod", unexpected_chmod)
    Config.defaults()


@pytest.mark.parametrize("already_exists", [False, True])
def test_app_directory_is_private(tmp_path, already_exists):
    app_dir = tmp_path / "streamrip"
    if already_exists:
        app_dir.mkdir(mode=0o755)
    # Override Click's directory lookup so this also covers macOS without
    # modifying the current user's actual application directory.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import click, sys; click.get_app_dir = lambda _: sys.argv[1]; "
            "import streamrip.config",
            str(app_dir),
        ],
        check=True,
    )
    assert mode(app_dir) == 0o700
