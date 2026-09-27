from click.testing import CliRunner

from streamrip.rip.cli import UPGRADE_COMMAND, is_newer_version, rip


def test_version_comparison_is_numeric():
    assert is_newer_version("2.10.0", "2.9.9")
    assert is_newer_version("v2.4", "2.3.0")
    assert not is_newer_version("2.3.0", "2.3.0")
    assert not is_newer_version("2.2.0", "2.3.0")
    assert not is_newer_version(None, "2.3.0")


def test_upgrade_command_installs_from_the_fork():
    assert "github.com/Stensel8/streamrip" in UPGRADE_COMMAND
    assert "pip install streamrip" not in UPGRADE_COMMAND


def test_help_lists_commands(tmp_path):
    result = CliRunner().invoke(
        rip, ["--config-path", str(tmp_path / "config.toml"), "--help"]
    )
    assert result.exit_code == 0
    for command in ("url", "file", "search", "lastfm", "id", "repair", "config"):
        assert command in result.output


def test_codec_choice_accepts_opus_and_aiff(tmp_path):
    cfg = str(tmp_path / "config.toml")
    for codec in ("opus", "AIFF"):
        result = CliRunner().invoke(
            rip, ["--config-path", cfg, "-c", codec, "config", "path"]
        )
        assert result.exit_code == 0, result.output
    result = CliRunner().invoke(
        rip, ["--config-path", cfg, "-c", "wma", "config", "path"]
    )
    assert result.exit_code != 0
