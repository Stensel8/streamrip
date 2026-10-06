import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
import tomlkit
from click.testing import CliRunner

from streamrip import db
from streamrip.config import Config, set_user_defaults
from streamrip.rip.cli import _upgrade_command, is_newer_version, rip


def test_version_comparison_is_numeric():
    assert is_newer_version("2.10.0", "2.9.9")
    assert is_newer_version("v2.4", "2.3.0")
    assert not is_newer_version("2.3.0", "2.3.0")
    assert not is_newer_version("2.2.0", "2.3.0")
    assert not is_newer_version(None, "2.3.0")


def test_upgrade_command_installs_from_the_fork(monkeypatch):
    monkeypatch.setattr("streamrip.rip.cli.shutil.which", lambda _: "/path/to/uv")
    cmd = _upgrade_command("2.4.5")
    assert cmd.startswith(
        f'"/path/to/uv" pip install --python "{sys.executable}" --upgrade '
    )
    assert "github.com/Stensel8/streamrip" in cmd
    assert "pip install streamrip" not in cmd


def test_upgrade_command_uses_interpreter_pip_without_uv(monkeypatch):
    monkeypatch.setattr("streamrip.rip.cli.shutil.which", lambda _: None)
    cmd = _upgrade_command("2.4.5")
    assert cmd.startswith(f'"{sys.executable}" -m pip install --upgrade ')


def test_upgrade_command_pins_the_detected_release():
    # Not `dev` HEAD, which can be ahead of or behind the release just detected.
    assert _upgrade_command("2.4.5").endswith("@v2.4.5")


def test_help_lists_commands(tmp_path, capsys):
    result = CliRunner().invoke(
        rip, ["--config-path", str(tmp_path / "config.toml"), "--help"]
    )
    assert result.exit_code == 0
    output = _output(result, capsys)
    assert (
        "A fast, all-in-one scriptable music downloader for Qobuz, Deezer, Tidal, SoundCloud, and Spotify."
        in " ".join(output.split())
    )
    for command in ("url", "file", "search", "lastfm", "id", "repair", "config"):
        assert command in output


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


def test_database_clear_downloads_leaves_failed_alone(tmp_path, capsys):
    cfg, downloads, failed = _seeded_databases(tmp_path)
    result = _clear(cfg, "downloads", "-y")
    assert result.exit_code == 0, result.output
    assert downloads.all() == []
    assert len(failed.all()) == 1
    assert "Cleared 2 downloaded track(s)" in _output(result, capsys)


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


def test_database_clear_asks_first_and_can_be_declined(tmp_path, capsys):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    result = _clear(cfg, "downloads", input="n\n")
    assert result.exit_code == 0, result.output
    assert len(downloads.all()) == 2
    assert "Clear aborted" in _output(result, capsys)


def test_database_clear_when_confirmed(tmp_path):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    result = _clear(cfg, "downloads", input="y\n")
    assert result.exit_code == 0, result.output
    assert downloads.all() == []


def test_database_clear_with_nothing_to_clear(tmp_path, capsys):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    downloads.clear()
    result = _clear(cfg, "downloads")
    assert result.exit_code == 0, result.output
    assert "Nothing to clear" in _output(result, capsys)


def test_database_clear_rejects_an_unknown_table(tmp_path):
    cfg, downloads, _ = _seeded_databases(tmp_path)
    assert _clear(cfg, "everything", "-y").exit_code != 0
    assert len(downloads.all()) == 2


def test_database_browse_failed_lines_up_with_its_headers(tmp_path, capsys):
    cfg, _, _ = _seeded_databases(tmp_path)
    result = CliRunner().invoke(
        rip, ["--config-path", cfg, "database", "browse", "failed"]
    )
    assert result.exit_code == 0, result.output
    lines = _output(result, capsys).splitlines()
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
        AsyncMock(return_value=("99.0.0", None, True)),
    )
    _FakeMain.to_raise = raise_during_rip
    monkeypatch.setattr("streamrip.rip.cli.Main", _FakeMain)
    return CliRunner().invoke(
        rip,
        ["--config-path", str(tmp_path / "config.toml"), "url", "https://example"],
    )


def _output(result, capsys) -> str:
    """Everything the run printed, whichever capture it landed in.

    Rich's output goes to Click's capture or to pytest's, depending on how the
    tests are run (`log_cli` in pyproject.toml, or a Rich spinner earlier in
    the run, change which), so tests must not assume one.
    """
    return result.output + capsys.readouterr().out


def _printed(result, capsys) -> str:
    """The output, with Rich's line wrapping and spacing undone.

    A long path (a deep venv) wraps over lines.
    """
    return "".join(_output(result, capsys).split())


def test_update_notice_prints_after_a_clean_download(tmp_path, monkeypatch, capsys):
    result = _run_url_with_a_newer_version_available(tmp_path, monkeypatch, None)
    assert result.exit_code == 0, result.output
    out = _printed(result, capsys)
    assert "v99.0.0" in out
    assert "".join(sys.prefix.split()) in out


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
    out = _printed(result, capsys)
    assert "v99.0.0" in out
    assert "Stopped" in out
    assert "".join(sys.prefix.split()) in out


def test_upgrade_command_preserves_release_or_branch_origin():
    assert _upgrade_command("2.4.5", is_release=True).endswith("@v2.4.5")
    assert _upgrade_command("2.4.5", is_release=False).endswith("@HEAD")


@pytest.mark.parametrize("args", [["--help"], ["--version"]])
def test_update_failure_does_not_block_click(monkeypatch, capsys, args):
    check = AsyncMock(side_effect=RuntimeError("update check failed"))
    notice = MagicMock()
    monkeypatch.setattr("streamrip.rip.cli.latest_streamrip_version", check)
    monkeypatch.setattr("streamrip.rip.cli._print_update_notice", notice)

    result = CliRunner().invoke(rip, args)

    assert result.exit_code == 0
    output = result.output + capsys.readouterr().out
    assert ("Usage:" if args == ["--help"] else "version") in output
    check.assert_awaited_once()
    notice.assert_not_called()


@pytest.mark.parametrize("args", [["--help"], ["--version"]])
async def test_update_check_inside_event_loop_does_not_block_click(
    monkeypatch, capsys, args
):
    from inspect import CORO_CLOSED, getcoroutinestate

    check = AsyncMock()
    coroutine = check()
    notice = MagicMock()
    monkeypatch.setattr("streamrip.rip.cli.latest_streamrip_version", lambda: coroutine)
    monkeypatch.setattr("streamrip.rip.cli._print_update_notice", notice)

    result = CliRunner().invoke(rip, args)

    assert result.exit_code == 0
    output = result.output + capsys.readouterr().out
    assert ("Usage:" if args == ["--help"] else "version") in output
    assert getcoroutinestate(coroutine) == CORO_CLOSED
    check.assert_not_awaited()
    notice.assert_not_called()


def _broken_config(tmp_path) -> str:
    """A config this version cannot load: an option it has no field for."""
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    path.write_text(path.read_text().replace("[cli]", "[cli]\nbogus = 1", 1))
    return str(path)


@pytest.mark.parametrize(
    "command",
    [
        ["url", "https://example"],
        ["file", "{urls}"],
        ["id", "qobuz", "track", "1"],
        ["search", "qobuz", "album", "rumours"],
        ["lastfm", "https://www.last.fm/user/x/playlists/1"],
        ["repair"],
        ["database", "browse", "downloads"],
        ["database", "clear", "downloads", "-y"],
    ],
    ids=lambda command: " ".join(command[:2]),
)
def test_a_config_that_does_not_load_fails_the_command(tmp_path, capsys, command):
    # Exit 0 told scripts and cron that a run which did nothing had worked.
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example\n")
    cfg = _broken_config(tmp_path)

    result = CliRunner().invoke(
        rip,
        [
            "--config-path",
            cfg,
            *(arg.replace("{urls}", str(urls)) for arg in command),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Errorloadingconfig" in _printed(result, capsys)


def test_a_config_that_does_not_load_can_still_be_reset(tmp_path):
    # The commands that fix the config must keep working without one.
    cfg = _broken_config(tmp_path)

    for command in (["config", "path"], ["config", "reset", "-y"]):
        result = CliRunner().invoke(rip, ["--config-path", cfg, *command])
        assert result.exit_code == 0, result.output

    Config(cfg)  # loads now


class _RepairMain:
    """A Main whose retry downloads `downloaded`, marking them as the real one does."""

    downloaded: list[tuple[str, str]] = []
    downloads_at_rip: list = []

    def __init__(self, config):
        c = config.session.database
        self.database = db.Database(
            db.Downloads(c.downloads_path), db.Failed(c.failed_downloads_path)
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get_logged_in_client(self, _source):
        client = MagicMock()
        client.get_metadata = AsyncMock(side_effect=Exception("no album"))
        return client

    async def add_all_by_id(self, _items):
        pass

    async def resolve(self):
        pass

    async def rip(self):
        type(self).downloads_at_rip = self.database.downloads.all()
        for source, item_id in type(self).downloaded:
            self.database.set_downloaded(source, item_id)


def test_repair_matches_failures_by_source_and_id(tmp_path, monkeypatch, capsys):
    cfg, downloads, failed = _seeded_databases(tmp_path)
    failed.add(("qobuz", "track", "3"))  # the same id as the Tidal failure
    downloads.add(("3",))  # marked downloaded by a version that did that
    _RepairMain.downloaded = [("tidal", "3")]
    monkeypatch.setattr("streamrip.rip.cli.Main", _RepairMain)

    result = CliRunner().invoke(rip, ["--config-path", cfg, "repair", "-y"])

    assert result.exit_code == 0, result.output
    # The stale row went before the retry, or the retry would be skipped.
    assert ("3",) not in _RepairMain.downloads_at_rip
    # Tidal 3 was repaired and Qobuz 3 was not: it stays for the next run.
    assert failed.all() == [("qobuz", "track", "3")]
    assert "Repaired1/2item(s)" in _printed(result, capsys)


def test_repair_with_nothing_downloaded_keeps_every_failure(tmp_path, monkeypatch):
    cfg, _downloads, failed = _seeded_databases(tmp_path)
    _RepairMain.downloaded = []
    monkeypatch.setattr("streamrip.rip.cli.Main", _RepairMain)

    result = CliRunner().invoke(rip, ["--config-path", cfg, "repair", "-y"])

    assert result.exit_code == 0, result.output
    assert failed.all() == [("tidal", "track", "3")]


def test_database_browse_downloads_shows_each_rows_source(tmp_path, capsys):
    cfg, downloads, _failed = _seeded_databases(tmp_path)  # holds bare "1" and "2"
    downloads.add(("tidal_9",))

    result = CliRunner().invoke(
        rip, ["--config-path", cfg, "database", "browse", "downloads"]
    )

    assert result.exit_code == 0, result.output
    out = _printed(result, capsys)
    assert "Source" in out
    assert out.count("unknown") == 2  # the two rows from before sources
    assert "tidal" in out


def _config_with_no_update_check(tmp_path, value="true") -> str:
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    path.write_text(
        path.read_text().replace(
            "# no_update_check = true", f"no_update_check = {value}"
        )
    )
    return str(path)


def _update_check(monkeypatch) -> AsyncMock:
    check = AsyncMock(return_value=("99.0.0", None, True))
    monkeypatch.setattr("streamrip.rip.cli.latest_streamrip_version", check)
    return check


def test_the_update_check_runs_unless_the_config_turns_it_off(tmp_path, monkeypatch):
    check = _update_check(monkeypatch)
    cfg = str(tmp_path / "config.toml")
    set_user_defaults(cfg)

    result = CliRunner().invoke(rip, ["--config-path", cfg, "config", "path"])

    assert result.exit_code == 0, result.output
    check.assert_awaited_once()


@pytest.mark.parametrize("path_option", ["separate", "equals"])
@pytest.mark.parametrize("command", [["config", "path"], ["--help"], ["--version"]])
def test_no_update_check_in_the_config_skips_the_check(
    tmp_path, monkeypatch, command, path_option
):
    # --help and --version never reach the group's callback, where the config
    # is loaded, but they are checked too and so are skipped too.
    check = _update_check(monkeypatch)
    cfg = _config_with_no_update_check(tmp_path)
    option = (
        ["--config-path", cfg]
        if path_option == "separate"
        else [f"--config-path={cfg}"]
    )

    result = CliRunner().invoke(rip, [*option, *command])

    assert result.exit_code == 0, result.output
    check.assert_not_awaited()


@pytest.mark.parametrize("value", ["false", '"true"', "1"])
def test_only_a_real_true_turns_the_check_off(tmp_path, monkeypatch, value):
    check = _update_check(monkeypatch)
    cfg = _config_with_no_update_check(tmp_path, value)

    CliRunner().invoke(rip, ["--config-path", cfg, "config", "path"])

    check.assert_awaited_once()


def test_a_config_that_cannot_be_read_does_not_turn_the_check_off(
    tmp_path, monkeypatch
):
    check = _update_check(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is [not toml")

    CliRunner().invoke(rip, ["--config-path", str(cfg), "config", "path"])

    check.assert_awaited_once()


def test_an_update_notice_does_not_come_when_the_check_is_off(
    tmp_path, monkeypatch, capsys
):
    _update_check(monkeypatch)
    _FakeMain.to_raise = None
    monkeypatch.setattr("streamrip.rip.cli.Main", _FakeMain)
    cfg = _config_with_no_update_check(tmp_path)

    result = CliRunner().invoke(rip, ["--config-path", cfg, "url", "https://example"])

    assert result.exit_code == 0, result.output
    assert "v99.0.0" not in _printed(result, capsys)


@pytest.mark.parametrize("git_installed", [True, False])
def test_the_update_notice_says_it_needs_git(
    tmp_path, monkeypatch, capsys, git_installed
):
    # `pip install git+https://...` fails without it ("Cannot find command 'git'").
    monkeypatch.setattr(
        "streamrip.rip.cli.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" and git_installed else None,
    )

    result = _run_url_with_a_newer_version_available(tmp_path, monkeypatch, None)

    assert result.exit_code == 0, result.output
    out = _printed(result, capsys)
    assert "Thisneedsgit" in out
    assert ("notfound" in out) is not git_installed
    assert ("git-scm.com/downloads" in out) is not git_installed
