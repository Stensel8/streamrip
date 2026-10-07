"""`streamrip csv`: the file, the question where to search, and what it starts."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from click.testing import CliRunner

from streamrip.config import set_user_defaults
from streamrip.rip import cli
from streamrip.rip.cli import CSV_SOURCES, _ask_source, rip
from streamrip.rip.main import Main


@pytest.fixture
def run(tmp_path):
    """Invoke `streamrip csv ...` with downloading and logging in left out."""
    config = tmp_path / "config.toml"
    set_user_defaults(str(config))
    calls = {}

    def invoke(*args):
        with (
            patch("streamrip.rip.main.find_ffmpeg", return_value="/usr/bin/ffmpeg"),
            patch.object(Main, "resolve_csv", AsyncMock()) as resolve,
            patch.object(Main, "rip", AsyncMock()) as download,
        ):
            result = CliRunner().invoke(rip, ["--config-path", str(config), *args])
        calls["resolve"], calls["rip"] = resolve, download
        return result

    invoke.calls = calls
    return invoke


@pytest.fixture
def tracks_csv(tmp_path):
    path = tmp_path / "road trip.csv"
    path.write_text("title,artists,album\nGet Lucky,Daft Punk,Random Access Memories\n")
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
    asked = Mock(return_value="deezer")
    monkeypatch.setattr(cli, "_ask_source", asked)

    result = run("csv", tracks_csv)

    assert result.exit_code == 0, result.output
    asked.assert_called_once_with(1)  # told how many tracks there are
    assert run.calls["resolve"].await_args.args[2] == "deezer"


def test_without_a_terminal_there_is_nobody_to_ask(run, tracks_csv):
    result = run("csv", tracks_csv)  # CliRunner has no terminal

    assert result.exit_code == 2
    assert "--source" in result.output
    run.calls["resolve"].assert_not_awaited()


def test_the_question_lists_every_source_and_what_it_gives(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", Mock(isatty=lambda: True))
    console = Mock()
    monkeypatch.setattr(cli, "console", console)
    ask = Mock(return_value="qobuz")
    monkeypatch.setattr(cli.Prompt, "ask", ask)

    assert _ask_source(120) == "qobuz"

    shown = " ".join(str(call.args[0]) for call in console.print.call_args_list)
    assert "120" in shown
    for name, what in CSV_SOURCES.items():
        assert name in shown
        assert what in shown
    assert ask.call_args.kwargs["choices"] == list(CSV_SOURCES)


@pytest.mark.parametrize(
    "source", ["qobuz", "tidal", "deezer", "spotify", "soundcloud"]
)
def test_every_source_can_be_chosen(run, tracks_csv, source):
    assert run("csv", tracks_csv, "-s", source).exit_code == 0


def test_a_source_that_does_not_exist_is_refused(run, tracks_csv):
    result = run("csv", tracks_csv, "--source", "napster")

    assert result.exit_code == 2
    assert "napster" in result.output


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "No tracks found"),
        ("title,artists\n", "No tracks found"),
        (b"title,artists\nCaf\xe9,Band\n", "not UTF-8 encoded"),
    ],
    ids=["empty", "only a header", "not utf-8"],
)
def test_a_file_without_usable_tracks_ends_the_command_with_a_reason(
    run, tmp_path, content, message
):
    path = tmp_path / "bad.csv"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())

    result = run("csv", str(path), "--source", "tidal")

    assert result.exit_code == 1
    assert message in result.output
    run.calls["resolve"].assert_not_awaited()


def test_a_file_that_is_not_there_is_refused(run, tmp_path):
    result = run("csv", str(tmp_path / "nope.csv"), "--source", "tidal")

    assert result.exit_code == 2
    assert "does not exist" in result.output
