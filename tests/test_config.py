import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from streamrip import config as config_module
from streamrip.config import (
    BLANK_CONFIG_PATH,
    ArtistFilterConfig,
    ArtworkConfig,
    Config,
    ConfigData,
    ConversionConfig,
    DatabaseConfig,
    DeezerConfig,
    DownloadsConfig,
    FilepathsConfig,
    LastFmConfig,
    MetadataConfig,
    QobuzConfig,
    SoundcloudConfig,
    TidalConfig,
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


def _turn_on_no_update_check(path: Path) -> None:
    text = path.read_text()
    path.write_text(text.replace("# no_update_check = true", "no_update_check = true"))


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- loading ------------------------------------------------------------------


def test_the_sample_config_loads_into_its_sections():
    loaded = ConfigData.from_toml(SAMPLE_CONFIG.read_text(encoding="utf-8"))
    expected = {
        "downloads": DownloadsConfig(
            folder="test_folder",
            source_subdirectories=False,
            disc_subdirectories=True,
            max_connections=6,
            requests_per_minute=60,
            verify_ssl=True,
            lyrics=True,
        ),
        "qobuz": QobuzConfig(
            user_id="123456789",
            auth_token="test_token",
            app_id="12345",
            quality=3,
            download_booklets=True,
            secrets=["secret1", "secret2"],
        ),
        "tidal": TidalConfig(
            user_id="userid",
            country_code="countrycode",
            access_token="accesstoken",
            refresh_token="refreshtoken",
            token_expiry="tokenexpiry",
            quality=3,
            hires_client=False,
            client_id="",
            client_secret="",
            token_client_id="",
            hires_access_token="",
            hires_refresh_token="",
            hires_token_expiry="",
            hires_token_client_id="",
        ),
        "deezer": DeezerConfig(
            arl="testarl", quality=2, lower_quality_if_not_available=True
        ),
        "soundcloud": SoundcloudConfig(client_id="clientid", app_version="appversion"),
        "lastfm": LastFmConfig(source="qobuz", fallback_source=""),
        "filepaths": FilepathsConfig(
            add_singles_to_folder=False,
            folder_format="{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]",
            track_format="{tracknumber}. {artist} - {title}{explicit}",
            restrict_characters=False,
            truncate_to=120,
        ),
        "artwork": ArtworkConfig(
            embed=True,
            embed_size="large",
            embed_max_width=-1,
            save_artwork=True,
            saved_max_width=-1,
        ),
        "metadata": MetadataConfig(
            set_playlist_to_album=True,
            renumber_playlist_tracks=True,
            exclude=[],
            prefer_explicit=True,
        ),
        "artist_filters": ArtistFilterConfig(
            extras=False,
            repeats=False,
            non_albums=False,
            features=False,
            non_remaster=False,
        ),
        "database": DatabaseConfig(
            downloads_enabled=True,
            downloads_path="downloadspath",
            failed_downloads_enabled=True,
            failed_downloads_path="faileddownloadspath",
        ),
        "conversion": ConversionConfig(
            enabled=False,
            codec="ALAC",
            sampling_rate=48000,
            bit_depth=24,
            lossy_bitrate=320,
        ),
    }
    for section, values in expected.items():
        assert getattr(loaded, section) == values, section


def test_a_config_saved_with_a_byte_order_mark_loads(tmp_path):
    # Notepad's "UTF-8 with BOM", and PowerShell 5.1's Set-Content -Encoding UTF8.
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf" + SAMPLE_CONFIG.read_bytes())

    assert (
        Config(path).session.downloads.folder
        == Config(SAMPLE_CONFIG).session.downloads.folder
    )


def test_the_template_and_the_config_classes_agree():
    """A key the classes lack, or a field the template lacks, fails loading."""
    template = BLANK_CONFIG_PATH.read_text()
    ConfigData.from_toml(template)

    with pytest.raises(TypeError, match="bogus_option"):
        ConfigData.from_toml(
            template.replace("[cli]\n", "[cli]\nbogus_option = 1\n", 1)
        )
    with pytest.raises(TypeError, match="max_search_results"):
        ConfigData.from_toml(
            re.sub(r"^max_search_results = .*\n", "", template, flags=re.MULTILINE)
        )


# --- the default downloads folder ---------------------------------------------


def test_user_defaults_download_to_the_music_folder(tmp_path, monkeypatch):
    fake_music = tmp_path / "home" / "Music"
    monkeypatch.setattr(config_module, "DEFAULT_DOWNLOADS_FOLDER", fake_music)
    path = tmp_path / "config.toml"

    set_user_defaults(path)

    assert Config(path).session.downloads.folder == str(fake_music)


@pytest.mark.parametrize(
    ("run", "found"),
    [
        (
            MagicMock(
                return_value=subprocess.CompletedProcess([], 0, stdout="/data/Muziek\n")
            ),
            Path("/data/Muziek"),  # moved, or named in the user's language
        ),
        (MagicMock(return_value=subprocess.CompletedProcess([], 1, stdout="")), None),
        (MagicMock(side_effect=FileNotFoundError("no xdg-user-dir")), None),
    ],
    ids=["set up", "xdg-user-dirs not set up", "no xdg-user-dir binary"],
)
def test_xdg_music_dir(monkeypatch, run, found):
    monkeypatch.setattr(config_module.subprocess, "run", run)

    assert config_module._xdg_music_dir() == found
    assert run.call_args.args[0] == ["xdg-user-dir", "MUSIC"]


@pytest.mark.parametrize(
    ("error", "found"),
    [(None, Path("D:\\Muziek")), (OSError("no such key"), None)],
    ids=["moved or localized", "no such registry key"],
)
def test_windows_music_dir(monkeypatch, error, found):
    winreg = MagicMock()
    winreg.QueryValueEx.return_value = ("D:\\Muziek", 1)
    winreg.OpenKey.return_value.__enter__ = lambda self: winreg
    winreg.OpenKey.return_value.__exit__ = lambda *a: None
    winreg.OpenKey.side_effect = error
    monkeypatch.setitem(sys.modules, "winreg", winreg)

    assert config_module._windows_music_dir() == found


@pytest.mark.parametrize(
    ("platform", "asked"),
    [
        ("win32", "_windows_music_dir"),
        ("linux", "_xdg_music_dir"),
        ("win32", None),
        ("linux", None),
        ("darwin", None),  # always ~/Music, whatever the language
    ],
)
def test_default_music_dir_asks_the_platform_then_falls_back_to_home_music(
    monkeypatch, platform, asked
):
    monkeypatch.setattr(config_module.sys, "platform", platform)
    monkeypatch.setattr(config_module, "_windows_music_dir", lambda: None)
    monkeypatch.setattr(config_module, "_xdg_music_dir", lambda: None)
    if asked:
        monkeypatch.setattr(config_module, asked, lambda: Path("/found"))

    expected = Path("/found") if asked else config_module.HOME / "Music"
    assert config_module._default_music_dir() == expected


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


@pytest.mark.parametrize("turned_on", [False, True])
def test_saving_leaves_an_option_as_the_user_wrote_it(config_path, turned_on):
    if turned_on:
        _turn_on_no_update_check(config_path)
    config = Config(config_path)
    assert config.session.cli.no_update_check is turned_on
    config.file.deezer.arl = "synthetic-cookie"
    config.file.set_modified()

    config.save_file()

    if not turned_on:
        # Written beside the commented line, it would make uncommenting that a
        # duplicate key, and the config would not load.
        saved = config_path.read_text()
        assert "\n# no_update_check = true\n" in saved
        assert "\nno_update_check" not in saved
        _turn_on_no_update_check(config_path)
    assert Config(config_path).session.cli.no_update_check is True


def test_the_new_contents_appear_in_one_step(config_path, monkeypatch):
    config = _new_token(config_path)
    original = config_path.read_bytes()
    real_replace = os.replace
    seen = []

    def replace(src, dst):
        # Right up to the swap, the config is still the old one, complete.
        seen.append(config_path.read_bytes() == original)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)

    config.save_file()

    assert seen == [True]
    assert Config(config_path).file.deezer.arl == "new-synthetic-cookie"
    assert [p.name for p in config_path.parent.iterdir()] == ["config.toml"]


@pytest.mark.parametrize(
    ("failing_step", "error"),
    [
        ("fsync", OSError("No space left on device")),
        ("replace", OSError("No space left on device")),
        # Ctrl-C, which `except Exception` would let through with the temp
        # file left behind.
        ("replace", KeyboardInterrupt()),
    ],
    ids=["fsync", "replace", "interrupted"],
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
    assert Config(real).file.deezer.arl == "new-synthetic-cookie"
    assert sorted(p.name for p in repo.iterdir()) == ["config.toml"]


def test_a_config_can_be_made_in_a_folder_that_is_not_there_yet(tmp_path):
    path = tmp_path / "new" / "folder" / "config.toml"
    set_user_defaults(path)
    assert path.is_file()


# --- private files: credentials must stay private whatever the umask ----------


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
    @pytest.mark.parametrize("existing_mode", [None, 0o600, 0o644, 0o666])
    def test_creation_and_reset_are_private(self, tmp_path, existing_mode):
        path = tmp_path / "config.toml"
        if existing_mode is not None:
            path.write_text("old contents" * 1000)
            path.chmod(existing_mode)
        parent_mode = mode(tmp_path)

        set_user_defaults(path)

        assert mode(path) == 0o600
        assert mode(tmp_path) == parent_mode
        assert Config(path).file.deezer.arl == ""

    @pytest.mark.login
    def test_loading_existing_credentials_repairs_permissions(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(BLANK_CONFIG_PATH.read_text())
        path.chmod(0o644)
        original = path.read_bytes()

        Config(path).save_file()  # Nothing was marked modified.

        assert mode(path) == 0o600
        assert path.read_bytes() == original

    @pytest.mark.login
    @pytest.mark.parametrize("remove_file", [False, True])
    def test_saving_credentials_secures_existing_or_recreated_file(
        self, config_path, remove_file
    ):
        config = Config(config_path)
        config.file.qobuz.auth_token = "synthetic-qobuz-token"
        config.file.deezer.arl = "synthetic-deezer-cookie"
        config.file.tidal.refresh_token = "synthetic-tidal-token"
        config.file.set_modified()
        if remove_file:
            config_path.unlink()
        else:
            config_path.chmod(0o666)  # Permissions can change after loading.

        config.save_file()

        assert mode(config_path) == 0o600
        saved = Config(config_path).file
        assert saved.qobuz.auth_token == "synthetic-qobuz-token"
        assert saved.deezer.arl == "synthetic-deezer-cookie"
        assert saved.tidal.refresh_token == "synthetic-tidal-token"

    @pytest.mark.parametrize("operation", ["save", "reset"])
    def test_permission_failure_aborts_before_changing_contents(
        self, config_path, monkeypatch, operation
    ):
        config = _new_token(config_path)
        config_path.chmod(0o644)
        original = config_path.read_bytes()
        descriptors = []

        def deny_chmod(fd, permissions):
            descriptors.append(fd)
            raise PermissionError("Cannot restrict permissions")

        monkeypatch.setattr(os, "fchmod", deny_chmod)
        save = (
            config.save_file
            if operation == "save"
            else lambda: set_user_defaults(config_path)
        )
        with pytest.raises(PermissionError, match="Cannot restrict permissions"):
            save()

        assert config_path.read_bytes() == original
        assert descriptors
        for fd in descriptors:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)  # Failed saves must also close their file descriptor.

    def test_loading_defaults_does_not_chmod_packaged_template(self, monkeypatch):
        def unexpected_chmod(*args):
            pytest.fail("Loading defaults must not chmod the packaged template")

        monkeypatch.setattr(os, "fchmod", unexpected_chmod)
        Config.defaults()

    @pytest.mark.parametrize("already_exists", [False, True])
    def test_app_directory_is_private(self, tmp_path, already_exists):
        app_dir = tmp_path / "streamrip"
        if already_exists:
            app_dir.mkdir(mode=0o755)
        # Override Click's directory lookup so this also covers macOS without
        # modifying the current user's actual application directory.
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
