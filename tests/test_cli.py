import asyncio
from unittest.mock import AsyncMock

import tomlkit
from click.testing import CliRunner

from streamrip import db
from streamrip.config import set_user_defaults
from streamrip.rip.cli import _upgrade_command, is_newer_version, rip


def test_version_comparison_is_numeric():
    assert is_newer_version("2.10.0", "2.9.9")
    assert is_newer_version("v2.4", "2.3.0")
    assert not is_newer_version("2.3.0", "2.3.0")
    assert not is_newer_version("2.2.0", "2.3.0")
    assert not is_newer_version(None, "2.3.0")


def test_upgrade_command_installs_from_the_fork():
    cmd = _upgrade_command("2.4.5")
    assert "github.com/Stensel8/streamrip" in cmd
    assert "pip install streamrip" not in cmd


def test_upgrade_command_pins_the_detected_release():
    # Not `dev` HEAD, which can be ahead of or behind the release just detected.
    assert _upgrade_command("2.4.5").endswith("@v2.4.5")


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


def test_database_browse_failed_lines_up_with_its_headers(tmp_path):
    cfg, _, _ = _seeded_databases(tmp_path)
    result = CliRunner().invoke(
        rip, ["--config-path", cfg, "database", "browse", "failed"]
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    header = next(line for line in lines if "Source" in line)
    row = next(line for line in lines if "tidal" in line)
    cells = dict(
        zip(
            [c.strip() for c in header.split("┃")[1:-1]],
            [c.strip() for c in row.split("│")[1:-1]],
        )
    )
    assert cells == {"Row": "00", "Source": "tidal", "Media Type": "track", "ID": "3"}


def test_file_keeps_url_order_when_dropping_repeats(tmp_path, monkeypatch):
    added = []

    class FakeMain:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def add_all(self, urls):
            added.extend(urls)

        async def resolve(self):
            pass

        async def rip(self):
            pass

    monkeypatch.setattr("streamrip.rip.cli.Main", FakeMain)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://c\nhttps://a\nhttps://c\nhttps://b\n")
    result = CliRunner().invoke(
        rip, ["--config-path", str(tmp_path / "config.toml"), "file", str(urls)]
    )
    assert result.exit_code == 0, result.output
    assert added == ["https://c", "https://a", "https://b"]


class _FakeMain:
    """A Main whose download step optionally raises, like a cancelled one."""

    to_raise: BaseException | None = None

    def __init__(self, _config):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def add_all(self, urls):
        pass

    async def resolve(self):
        pass

    async def rip(self):
        if self.to_raise is not None:
            raise self.to_raise


def _run_url_with_a_newer_version_available(tmp_path, monkeypatch, raise_during_rip):
    monkeypatch.setattr(
        "streamrip.rip.cli.latest_streamrip_version",
        AsyncMock(return_value=("99.0.0", None)),
    )
    _FakeMain.to_raise = raise_during_rip
    monkeypatch.setattr("streamrip.rip.cli.Main", _FakeMain)
    return CliRunner().invoke(
        rip,
        ["--config-path", str(tmp_path / "config.toml"), "url", "https://example"],
    )


def test_update_notice_prints_after_a_clean_download(tmp_path, monkeypatch, capsys):
    result = _run_url_with_a_newer_version_available(tmp_path, monkeypatch, None)
    assert result.exit_code == 0, result.output
    # Rich's console writes straight to the real stdout, not Click's
    # result.output capture -- pytest's own capsys catches that instead.
    assert "v99.0.0" in capsys.readouterr().out


def test_update_notice_still_prints_when_the_download_is_cancelled(
    tmp_path, monkeypatch, capsys
):
    """Ctrl-C during a download must not also cancel the update notice.

    The notice is printed after the download step (see main_session in
    rip/cli.py) so search's screen-clearing picker can't wipe it out --
    but a cancelled/failed download raises through that same point, so
    printing it only has to happen in a finally, or this exact case
    (observed live 2026-09-30) silently drops it.
    """
    result = _run_url_with_a_newer_version_available(
        tmp_path, monkeypatch, asyncio.CancelledError()
    )
    assert result.exit_code == 0, result.output
    out = capsys.readouterr().out
    assert "v99.0.0" in out
    assert "Stopped" in out
