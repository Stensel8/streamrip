"""The `streamrip` command: its options, subcommands, update check, and how it stops."""

import asyncio
import json
import os
import re
import signal
import sys
from inspect import CORO_CLOSED, getcoroutinestate
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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
    SOURCES,
    _ask_source,
    _is_uv_tool_install,
    _upgrade_command,
    coro,
    is_newer_version,
    latest_streamrip_version,
    rip,
)
from streamrip.rip.main import Main
from streamrip.rip.notices import MUSIC_SYNC, notice_for
from streamrip.rip.stopping import terminate

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="asyncio signal handlers only exist on POSIX"
)


@pytest.fixture
def cfg(tmp_path) -> str:
    """A default config in tmp_path."""
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    return str(path)


def _rip(config, *args, **kwargs):
    """`streamrip --config-path config *args`."""
    return CliRunner().invoke(rip, ["--config-path", str(config), *args], **kwargs)


def _output(result, capsys) -> str:
    """Everything the run printed, whichever capture it landed in.

    Rich's output goes to Click's capture or to pytest's, depending on how the
    tests are run (`log_cli` in pyproject.toml, or a Rich spinner earlier in
    the run, change which), so tests must not assume one. Click prints its own
    errors on stderr, which lands in pytest's capture the same way.
    """
    captured = capsys.readouterr()
    return result.output + captured.out + captured.err


def _printed(result, capsys) -> str:
    """The output, with Rich's line wrapping and spacing undone.

    A long path (a deep venv) wraps over lines.
    """
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


# --- usage and options --------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "shown"),
    [
        (["--help"], "Usage: streamrip [OPTIONS] COMMAND [ARGS]..."),
        (["csv", "--help"], "Usage: streamrip csv [OPTIONS] PATH"),
        (["csv"], "Try 'streamrip csv --help' for help."),
        (["nonexistent"], "Try 'streamrip --help' for help."),
    ],
    ids=["help", "command help", "usage error", "unknown command"],
)
def test_the_command_is_called_streamrip_in_every_usage_line(cfg, capsys, args, shown):
    output = _output(_rip(cfg, *args), capsys)
    assert shown in output
    assert "Usage: rip" not in output
    assert "'rip " not in output


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["search", "artist", "tidal", "linkin park"], "'artist' is not one of"),
        (["search", "tidal", "artists", "linkin park"], "'artists' is not one of"),
        (["id", "album", "tidal", "234793"], "'album' is not one of"),
        (["database", "clear", "everything", "-y"], "'everything' is not one of"),
        (
            ["search", "--first", "-o", "{tmp}/out.json", "tidal", "track", "x"],
            "--first or --output-file",
        ),
    ],
)
def test_a_usage_error_says_what_is_wrong(cfg, tmp_path, capsys, args, said):
    result = _rip(cfg, *(arg.replace("{tmp}", str(tmp_path)) for arg in args))
    assert result.exit_code == 2
    assert said in _output(result, capsys)


def test_codec_choice_accepts_opus_and_aiff(tmp_path):
    cfg = str(tmp_path / "config.toml")
    for codec in ("opus", "AIFF"):
        result = _rip(cfg, "-c", codec, "config", "path")
        assert result.exit_code == 0, result.output
    assert _rip(cfg, "-c", "wma", "config", "path").exit_code != 0


# --- the config ---------------------------------------------------------------


def _break(cfg) -> str:
    """Make the config one this version cannot load: an option it has no field for."""
    path = Path(cfg)
    path.write_text(path.read_text().replace("[cli]", "[cli]\nbogus = 1", 1))
    return cfg


@pytest.mark.parametrize(
    "command",
    [
        ["url", "https://example"],
        ["file", "{urls}"],
        ["id", "qobuz", "track", "1"],
        ["search", "qobuz", "album", "rumours"],
        ["lastfm", "https://www.last.fm/user/x/playlists/1"],
        ["csv", "{urls}", "--source", "tidal"],
        ["repair"],
        ["database", "browse", "downloads"],
        ["database", "clear", "downloads", "-y"],
    ],
    ids=lambda command: " ".join(command[:2]),
)
def test_a_config_that_does_not_load_fails_the_command(cfg, tmp_path, capsys, command):
    # Exit 0 told scripts and cron that a run which did nothing had worked.
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example\n")

    result = _rip(_break(cfg), *(arg.replace("{urls}", str(urls)) for arg in command))

    assert result.exit_code == 1, result.output
    assert "Errorloadingconfig" in _printed(result, capsys)


def test_a_config_that_does_not_load_can_still_be_reset(cfg):
    # The commands that fix the config must keep working without one.
    _break(cfg)
    for command in (["config", "path"], ["config", "reset", "-y"]):
        result = _rip(cfg, *command)
        assert result.exit_code == 0, result.output

    Config(Path(cfg))  # loads now


def test_config_path_prints_only_the_path_even_for_a_new_config(tmp_path):
    """Scripts read it; the notice that a config was created goes to stderr."""
    cfg = tmp_path / "config.toml"
    result = _rip(cfg, "config", "path")
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{cfg}\n"
    assert "creating default config" in result.stderr


# --- the databases ------------------------------------------------------------


@pytest.fixture
def seeded(cfg, tmp_path):
    """The config with its databases in tmp_path (never the real config dir),
    holding two downloaded tracks and one failed download.
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
    ("args", "answer", "left", "said"),
    [
        (["downloads", "-y"], None, (0, 1), "Cleared 2 downloaded tracks"),
        (["Downloads", "-y"], None, (0, 1), "Cleared"),  # any case
        (["all", "-y"], None, (0, 0), "Cleared"),
        (["downloads"], "y\n", (0, 1), "Cleared"),
        (["downloads"], "n\n", (2, 1), "Clear aborted"),
    ],
    ids=["downloads", "Downloads", "all", "confirmed", "declined"],
)
def test_database_clear(seeded, capsys, args, answer, left, said):
    cfg, downloads, failed = seeded
    result = _rip(cfg, "database", "clear", *args, input=answer)
    assert result.exit_code == 0, result.output
    assert (len(downloads.all()), len(failed.all())) == left
    assert said in _output(result, capsys)


def test_database_clear_with_nothing_to_clear(seeded, capsys):
    cfg, downloads, _ = seeded
    downloads.clear()
    result = _rip(cfg, "database", "clear", "downloads")
    assert result.exit_code == 0, result.output
    assert "Nothing to clear" in _output(result, capsys)


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
    cfg, downloads, _ = seeded  # holds qobuz_1 and tidal_2
    downloads.add(("soundcloud_9",))

    result = _rip(cfg, "database", "browse", "downloads")

    assert result.exit_code == 0, result.output
    out = _printed(result, capsys)
    assert "Source" in out
    for source in ("qobuz", "tidal", "soundcloud"):
        assert source in out


# --- file and csv -------------------------------------------------------------


def test_file_keeps_url_order_when_dropping_repeats(tmp_path, fake_main):
    urls = tmp_path / "urls.txt"
    urls.write_text("https://c\nhttps://a\nhttps://c\nhttps://b\n")
    result = _rip(tmp_path / "config.toml", "file", str(urls))
    assert result.exit_code == 0, result.output
    assert fake_main.added == ["https://c", "https://a", "https://b"]


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


TRACKS_CSV = "title,artists,album\nGet Lucky,Daft Punk,Random Access Memories\n"


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


@pytest.mark.parametrize(
    "source", ["qobuz", "tidal", "deezer", "spotify", "soundcloud"]
)
def test_every_source_can_be_chosen(run, tracks_csv, source):
    assert run("csv", tracks_csv, "-s", source).exit_code == 0


def test_the_source_is_asked_when_it_is_left_out(run, tracks_csv, monkeypatch):
    asked = Mock(return_value="deezer")
    monkeypatch.setattr(cli_module, "_ask_source", asked)

    result = run("csv", tracks_csv)

    assert result.exit_code == 0, result.output
    asked.assert_called_once_with(1)  # told how many tracks there are
    assert run.calls["resolve"].await_args.args[2] == "deezer"


def test_the_question_lists_every_source_and_what_it_gives(monkeypatch):
    monkeypatch.setattr(cli_module.sys, "stdin", Mock(isatty=lambda: True))
    screen = Mock()
    monkeypatch.setattr(cli_module, "console", screen)
    ask = Mock(return_value="qobuz")
    monkeypatch.setattr(cli_module.Prompt, "ask", ask)

    assert _ask_source(120) == "qobuz"

    shown = " ".join(str(call.args[0]) for call in screen.print.call_args_list)
    assert "120" in shown
    for name, what in SOURCES.items():
        assert name in shown
        assert what in shown
    assert ask.call_args.kwargs["choices"] == list(SOURCES)


@pytest.mark.parametrize(
    ("content", "source", "code", "said"),
    [
        ("", "tidal", 1, "No tracks found"),
        ("title,artists\n", "tidal", 1, "No tracks found"),
        (b"title,artists\nCaf\xe9,Band\n", "tidal", 1, "not UTF-8 encoded"),
        (None, "tidal", 2, "does not exist"),
        (TRACKS_CSV, "napster", 2, "napster"),
        (TRACKS_CSV, None, 2, "--source"),  # CliRunner has no terminal to ask on
    ],
    ids=["empty", "only a header", "not utf-8", "no file", "no such source", "no tty"],
)
def test_a_csv_run_that_cannot_go_ahead_says_why(
    run, tmp_path, capsys, content, source, code, said
):
    path = tmp_path / "list.csv"
    if content is not None:
        path.write_bytes(content if isinstance(content, bytes) else content.encode())

    result = run("csv", str(path), *(["--source", source] if source else []))

    assert result.exit_code == code
    assert said in _output(result, capsys)
    run.calls["resolve"].assert_not_awaited()


# --- repair -------------------------------------------------------------------


class _RepairMain:
    """A Main whose retry downloads `downloaded`, marking them as the real one does."""

    downloaded: list[tuple[str, str]] = []
    downloads_at_rip: list = []

    def __init__(self, config):
        c = config.session.database
        self.database = db.Database(
            # As Main does: a table turned off is a Dummy.
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


def _repair(monkeypatch, cfg, downloaded, *options):
    _RepairMain.downloaded = downloaded
    monkeypatch.setattr("streamrip.rip.cli.Main", _RepairMain)
    return _rip(cfg, *options, "repair", "-y")


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


@pytest.mark.parametrize(
    ("options", "downloaded", "left", "said"),
    [
        (["--no-db"], [("tidal", "3")], [], "Repaired1of1item."),
        ([], [], [("tidal", "track", "3")], "Repaired0of1item."),
    ],
    ids=["without the downloads db", "nothing downloaded"],
)
def test_repair_keeps_only_what_failed_again(
    seeded, monkeypatch, capsys, options, downloaded, left, said
):
    cfg, _, failed = seeded
    result = _repair(monkeypatch, cfg, downloaded, *options)
    assert result.exit_code == 0, result.output
    assert failed.all() == left
    assert said in _printed(result, capsys)


# --- the update check ---------------------------------------------------------


def test_version_comparison_is_numeric():
    assert is_newer_version("2.10.0", "2.9.9")
    assert is_newer_version("v2.4", "2.3.0")
    assert not is_newer_version("2.3.0", "2.3.0")
    assert not is_newer_version("2.2.0", "2.3.0")
    assert not is_newer_version(None, "2.3.0")


GIT = "git+https://github.com/Stensel8/streamrip.git"
WHEEL = "https://example/streamrip.whl"


@pytest.mark.parametrize(
    ("uv", "uv_tool", "kwargs", "command"),
    [
        (
            "/uv",
            False,
            {},
            f'"/uv" pip install --python "{sys.executable}" --upgrade {GIT}@v2.4.5',
        ),
        (None, False, {}, f'"{sys.executable}" -m pip install --upgrade {GIT}@v2.4.5'),
        (
            "/uv",
            False,
            {"is_release": False},
            f'"/uv" pip install --python "{sys.executable}" --upgrade {GIT}@HEAD',
        ),
        ("/uv", True, {"wheel_url": WHEEL}, f'"/uv" tool install --upgrade "{WHEEL}"'),
        # install.sh's own .venv (or any other) has to be upgraded through the
        # Python already running: a uv tool venv elsewhere is not the install
        # in use, printed as sys.prefix in the notice.
        (
            "/uv",
            False,
            {"wheel_url": WHEEL},
            f'"/uv" pip install --python "{sys.executable}" --upgrade {GIT}@v2.4.5',
        ),
        (
            None,
            True,
            {"wheel_url": WHEEL},
            f'"{sys.executable}" -m pip install --upgrade {GIT}@v2.4.5',
        ),
    ],
    ids=[
        "uv",
        "pip without uv",
        "the default branch",
        "the wheel in a uv tool install",
        "no wheel outside one",
        "no wheel without uv",
    ],
)
def test_upgrade_command(monkeypatch, uv, uv_tool, kwargs, command):
    monkeypatch.setattr(cli_module.shutil, "which", lambda _: uv)
    monkeypatch.setattr(cli_module, "_is_uv_tool_install", lambda: uv_tool)
    assert _upgrade_command("2.4.5", **kwargs) == command


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("/home/sten/.local/share/uv/tools/streamrip", True),
        ("/home/sten/Documents/GitHub/streamrip/.venv", False),
    ],
    ids=["a uv tool venv", "an ordinary venv"],
)
def test_is_uv_tool_install_reads_the_venv_s_own_parent_folder(
    monkeypatch, prefix, expected
):
    monkeypatch.setattr(cli_module.sys, "prefix", prefix)
    assert _is_uv_tool_install() is expected


async def test_the_update_check_gives_up_on_a_silent_network(monkeypatch):
    async def no_answer(*_):
        await asyncio.sleep(60)

    monkeypatch.setattr(cli_module, "UPDATE_CHECK_TIMEOUT", 0.3)
    monkeypatch.setattr(cli_module, "_latest_release", no_answer)
    monkeypatch.setattr(cli_module, "_default_branch_version", no_answer)

    async with asyncio.timeout(2):
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
        (
            {
                RELEASES: _reply(status=404),
                "/pyproject.toml": _reply(text='version = "9.0.0"\n'),
            },
            None,
            ("9.0.0", None, False, "github.com", None),
        ),
        (None, None, (__version__, None, False, "github.com", None)),
    ],
    ids=[
        "github first",
        "opencommit when github is down",
        "the default branch without a release",
        "nothing answers",
    ],
)
async def test_where_the_update_check_finds_the_latest_version(
    serve, monkeypatch, github, opencommit, found
):
    sources = []
    for host, routes in (("github.com", github), ("opencommit.eu", opencommit)):
        sources.append(_source(host, None if routes is None else await serve(routes)))
    monkeypatch.setattr(cli_module, "_UPDATE_SOURCES", tuple(sources))

    assert await latest_streamrip_version() == found


@pytest.mark.parametrize("args", [["--help"], ["--version"]])
def test_update_failure_does_not_block_click(monkeypatch, capsys, args):
    check = AsyncMock(side_effect=RuntimeError("update check failed"))
    notice = MagicMock()
    monkeypatch.setattr("streamrip.rip.cli.latest_streamrip_version", check)
    monkeypatch.setattr("streamrip.rip.cli._print_update_notice", notice)

    result = CliRunner().invoke(rip, args)

    assert result.exit_code == 0
    assert ("Usage:" if args == ["--help"] else "version") in _output(result, capsys)
    check.assert_awaited_once()
    notice.assert_not_called()


@pytest.mark.parametrize("args", [["--help"], ["--version"]])
async def test_update_check_inside_event_loop_does_not_block_click(
    monkeypatch, capsys, args
):
    check = AsyncMock()
    coroutine = check()
    notice = MagicMock()
    monkeypatch.setattr("streamrip.rip.cli.latest_streamrip_version", lambda: coroutine)
    monkeypatch.setattr("streamrip.rip.cli._print_update_notice", notice)

    result = CliRunner().invoke(rip, args)

    assert result.exit_code == 0
    assert ("Usage:" if args == ["--help"] else "version") in _output(result, capsys)
    assert getcoroutinestate(coroutine) == CORO_CLOSED
    check.assert_not_awaited()
    notice.assert_not_called()


def _update_check(monkeypatch) -> AsyncMock:
    check = AsyncMock(return_value=("99.0.0", None, True, "github.com", None))
    monkeypatch.setattr("streamrip.rip.cli.latest_streamrip_version", check)
    return check


def _no_update_check(cfg, value="true") -> str:
    """Set `no_update_check = value` in the config."""
    path = Path(cfg)
    text = path.read_text()
    path.write_text(
        text.replace("# no_update_check = true", f"no_update_check = {value}")
    )
    return cfg


@pytest.mark.parametrize(
    ("env", "setting", "checked"),
    [
        (None, None, True),
        ("1", None, False),
        ("true", None, False),
        ("yes", None, False),
        ("anything", None, False),
        ("", None, True),
        ("0", None, True),
        ("false", None, True),
        ("FALSE", None, True),
        ("no", None, True),
        # In the config, only a real true turns it off.
        (None, "false", True),
        (None, '"true"', True),
        (None, "1", True),
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


@pytest.mark.parametrize("path_option", ["separate", "equals"])
@pytest.mark.parametrize("command", [["config", "path"], ["--help"], ["--version"]])
def test_no_update_check_in_the_config_skips_the_check(
    cfg, monkeypatch, command, path_option
):
    # --help and --version never reach the group's callback, where the config
    # is loaded, but they are checked too and so are skipped too.
    check = _update_check(monkeypatch)
    _no_update_check(cfg)
    option = (
        ["--config-path", cfg]
        if path_option == "separate"
        else [f"--config-path={cfg}"]
    )

    result = CliRunner().invoke(rip, [*option, *command])

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


# --- the update notice --------------------------------------------------------


@pytest.mark.parametrize(
    ("to_raise", "git"),
    [(None, True), (None, False), (asyncio.CancelledError(), True)],
    ids=["after a download", "without git", "after a cancelled download"],
)
def test_the_update_notice(tmp_path, monkeypatch, capsys, fake_main, to_raise, git):
    # Printed after the download, so search's screen-clearing picker can't wipe
    # it out, and in a finally: Ctrl-C during a download raises through there.
    _update_check(monkeypatch)
    fake_main.to_raise = to_raise
    # `pip install git+https://...` fails without git ("Cannot find command 'git'").
    monkeypatch.setattr(
        "streamrip.rip.cli.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" and git else None,
    )

    result = _rip(tmp_path / "config.toml", "url", "https://example")

    assert result.exit_code == 0, result.output
    out = _printed(result, capsys)
    assert "v99.0.0" in out
    assert "".join(sys.prefix.split()) in out
    assert "Thisneedsgit" in out
    assert ("notfound" in out) is not git
    assert ("git-scm.com/downloads" in out) is not git
    if to_raise:
        assert "Stopped" in out


def test_an_update_notice_does_not_come_when_the_check_is_off(
    cfg, monkeypatch, capsys, fake_main
):
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
    """A command that is sent `signum` while it waits, and records how it ended.

    With `swallow`, it catches the cancellation, as a screen may.
    """

    @coro
    async def command():
        # A signal without a handler would end the whole test run: look first.
        assert signal.getsignal(signum) not in {signal.SIG_DFL, signal.SIG_IGN}
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
        (signal.SIGINT, False, None),
    ],
    ids=["SIGTERM exits 143", "even when the command swallows it", "Ctrl-C ends"],
)
def test_a_signal_cancels_the_command_after_its_cleanup(signum, swallow, code):
    seen = {}
    assert _exit_code(_waiting_command(signum, seen, swallow)) == code
    assert seen["cleaned_up"]
    assert seen["sigterm"] is signal.SIG_DFL
    assert seen["sigint"] is signal.default_int_handler


@posix_only
def test_the_handlers_are_gone_once_the_command_has_ended():
    previous = signal.getsignal(signal.SIGTERM)

    @coro
    async def quick():
        return None

    quick()

    assert signal.getsignal(signal.SIGTERM) == previous


@posix_only
def test_a_plain_command_ends_on_sigterm_only_while_it_runs(cfg, monkeypatch):
    """Commands without an event loop, and their prompts, need it too."""
    seen = {}

    def probe():
        seen["during"] = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(rip.commands["config"].commands["path"], "callback", probe)
    before = signal.getsignal(signal.SIGTERM)

    result = _rip(cfg, "config", "path")

    assert result.exit_code == 0, result.output
    assert seen["during"] is terminate
    assert signal.getsignal(signal.SIGTERM) == before


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
    """rip installs rich's handler process-wide; put the original back."""
    saved = sys.excepthook
    yield
    sys.excepthook = saved


FAKE_ARL = "f00dfeedc0ffee" * 8


def _login(arl):
    """Stand-in for DeezerClient.login: fails with the ARL in a local."""
    raise RuntimeError("Deezer login failed")


@pytest.mark.usefixtures("_restore_excepthook")
@pytest.mark.parametrize("verbose", [True, False])
def test_traceback_does_not_print_local_variables(tmp_path, verbose):
    """A traceback is what gets pasted into bug reports.

    Under -v, rich's handler rendered every frame's locals, so a failed login
    printed `arl = '<the ARL>'`, and any frame holding a Config printed it
    again through its repr.
    """
    args = ["--config-path", str(tmp_path / "config.toml"), "config", "path"]
    result = CliRunner().invoke(rip, ["-v", *args] if verbose else args)
    assert result.exit_code == 0, result.output

    try:
        _login(FAKE_ARL)
    except RuntimeError:
        exc_info = sys.exc_info()

    with console.capture() as capture:
        sys.excepthook(*exc_info)
    out = capture.get()

    assert "Deezer login failed" in out  # the traceback itself is still shown
    assert FAKE_ARL[:16] not in out


# --- Main: what a run reports, and the notices of a source --------------------


@pytest.fixture
def mock_main():
    """A Main with mock clients and no databases."""
    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
        patch("streamrip.rip.main.SpotifyClient"),
    ):
        yield Main(config)


def _main() -> Main:
    """A Main on the default config, with no databases."""
    config = Config.defaults()
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    return Main(config)


async def test_the_summary_of_a_run_counts_tracks_not_items(mock_main, caplog):
    """One artist is one item, but hundreds of tracks: say the latter."""

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


async def test_a_run_with_nothing_resolved_says_nothing(mock_main, caplog):
    # As a search written to a file is: no "Download completed: 0 tracks".
    await mock_main.rip()
    assert "Download completed" not in caplog.text


async def test_one_item_that_fails_to_resolve_does_not_stop_the_others(
    mock_main, caplog
):
    ok = MagicMock(resolve=AsyncMock(return_value="album"))
    broken = MagicMock(id="123", resolve=AsyncMock(side_effect=KeyError("tracks")))
    mock_main.pending = [broken, ok]

    await mock_main.resolve()

    assert mock_main.media == ["album"]
    assert "Error resolving 123: KeyError" in caplog.text


async def test_a_search_page_without_results_is_no_results(mock_main):
    mock_main.clients["deezer"].search = AsyncMock(return_value=[{"data": []}])
    await mock_main.search_take_first("deezer", "track", "nothing matches")
    assert mock_main.pending == []


async def test_one_bad_url_costs_only_itself(caplog):
    """A url streamrip can't queue (here a Tidal video) skips just that url."""
    main = _main()
    main.clients["tidal"].logged_in = True

    await main.add_all(
        ["https://tidal.com/browse/video/5678", "https://tidal.com/browse/album/1234"]
    )

    assert [type(p) for p in main.pending] == [PendingAlbum]
    assert "Error with https://tidal.com/browse/video/5678: NotImplementedError" in (
        caplog.text
    )


def test_spotify_says_it_is_lossy_and_points_to_music_sync():
    notice = notice_for("spotify")
    assert "lossy" in notice
    assert "YouTube Music" in notice
    assert "Music-Sync" in notice
    assert MUSIC_SYNC in notice
    assert "streamrip csv" in notice  # what to do with what Music-Sync exports


@pytest.mark.parametrize("source", ["qobuz", "tidal", "deezer", "soundcloud"])
def test_other_sources_have_nothing_to_say(source):
    assert notice_for(source) is None


async def test_a_source_is_announced_once_however_often_it_is_used():
    main = _main()
    for client in main.clients.values():
        client.logged_in = True

    with patch("streamrip.rip.main.console") as screen:
        for source in ("spotify", "spotify", "deezer", "qobuz", "deezer"):
            await main.get_logged_in_client(source)

    printed = [call.args[0] for call in screen.print.call_args_list]
    assert len(printed) == 1
    assert "Spotify" in printed[0]


# --- the version --------------------------------------------------------------


def test_pyproject_and_the_package_have_the_same_version():
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8")
    m = re.search(r'version\s*\=\s*"([\d\.]+)"', pyproject)
    assert m is not None
    assert m.group(1) == __version__
