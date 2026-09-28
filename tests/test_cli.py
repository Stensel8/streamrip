import tomlkit
from click.testing import CliRunner

from streamrip import db
from streamrip.config import set_user_defaults
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


def _seeded_databases(tmp_path):
    """A config whose databases live in tmp_path (never the real config dir),
    plus two downloaded tracks and one failed download.
    """
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    doc = tomlkit.parse(path.read_text())
    doc["database"]["downloads_path"] = str(tmp_path / "downloads.db")
    doc["database"]["failed_downloads_path"] = str(tmp_path / "failed.db")
    path.write_text(tomlkit.dumps(doc))

    downloads = db.Downloads(str(tmp_path / "downloads.db"))
    failed = db.Failed(str(tmp_path / "failed.db"))
    for track_id in ("1", "2"):
        downloads.add((track_id,))
    failed.add(("tidal", "track", "3"))
    return str(path), downloads, failed


def _clear(cfg, *args, **kwargs):
    return CliRunner().invoke(
        rip, ["--config-path", cfg, "database", "clear", *args], **kwargs
    )


def test_database_clear_downloads_leaves_failed_alone(tmp_path):
    cfg, downloads, failed = _seeded_databases(tmp_path)
    result = _clear(cfg, "downloads", "-y")
    assert result.exit_code == 0, result.output
    assert downloads.all() == []
    assert len(failed.all()) == 1
    assert "Cleared 2 downloaded track(s)" in result.output


def test_database_clear_all(tmp_path):
    cfg, downloads, failed = _seeded_databases(tmp_path)
    result = _clear(cfg, "all", "-y")
    assert result.exit_code == 0, result.output
    assert downloads.all() == []
    assert failed.all() == []


def test_database_clear_table_name_is_case_insensitive(tmp_path):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    assert _clear(cfg, "Downloads", "-y").exit_code == 0
    assert downloads.all() == []


def test_database_clear_asks_first_and_can_be_declined(tmp_path):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    result = _clear(cfg, "downloads", input="n\n")
    assert result.exit_code == 0, result.output
    assert len(downloads.all()) == 2
    assert "Clear aborted" in result.output


def test_database_clear_when_confirmed(tmp_path):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    result = _clear(cfg, "downloads", input="y\n")
    assert result.exit_code == 0, result.output
    assert downloads.all() == []


def test_database_clear_with_nothing_to_clear(tmp_path):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    downloads.clear()
    result = _clear(cfg, "downloads")
    assert result.exit_code == 0, result.output
    assert "Nothing to clear" in result.output


def test_database_clear_rejects_an_unknown_table(tmp_path):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    assert _clear(cfg, "everything", "-y").exit_code != 0
    assert len(downloads.all()) == 2
