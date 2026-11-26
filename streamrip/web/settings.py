"""The Settings page: every option of config.toml, laid out and explained as the
template does, and the file itself to edit by hand."""

import re
from dataclasses import dataclass, field
from functools import cache

from flask import (
    Blueprint,
    Response,
    jsonify,
)

from ..config import BLANK_CONFIG_PATH, ConfigData, reset_file, write_private
from ..sync.errors import ProviderError
from .common import get_config, sent, spa

settings_pages = Blueprint("settings", __name__, url_prefix="/settings")


# An option of the template, set or left commented out at its default.
_OPTION = re.compile(r"^(?:# )?([a-z_]+) = ")
# A value an "Options:" line lists: "fast" or 2, with what it means in brackets.
_CHOICE = re.compile(r'^(?:"([^"]+)"|(-?\d+))(?: \((.+)\))?$')


@dataclass(slots=True)
class Option:
    section: str
    name: str
    group: str
    # The template's words about it, line by line.
    help: str
    # The values to choose from with their labels, where the template lists them.
    choices: list[tuple[str | int, str]]

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").capitalize()


@dataclass(slots=True)
class Section:
    name: str
    title: str
    about: str
    options: list[Option] = field(default_factory=list)


def _choices(text: str) -> list[tuple[str | int, str]]:
    """The values `text` (an "Options:" line) lists, or none where it says them
    in words ("a folder path")."""
    found = []
    for item in re.split(r",\s+(?![^()]*\))", text.strip().rstrip(",")):
        if (m := _CHOICE.match(item)) is None:
            return []
        value = m[1] if m[1] is not None else int(m[2])
        found.append((value, f"{value} ({m[3]})" if m[3] else str(value)))
    return found


def _help(lines: list[str]) -> str:
    """Comment lines as text: prose joined, a list ("  "auto"  small jobs ...")
    an item a line, with the lines that go on indented further."""
    text, item = "", 0  # the indent of the list item the lines are in; 0: prose
    for line in lines:
        indent = len(line) - len(line.lstrip())
        if indent >= 2 and (not item or indent <= item):
            sep, item = "\n", indent  # a list item
        elif item and indent > item:
            sep = " "  # the item going on
        else:
            sep, item = (
                ("\n" if item else " "),
                0,
            )  # prose, after a list on a line of its own
        text += (sep if text else "") + line.strip()
    return text


@cache
def layout() -> list[Section]:
    """The sections and options of config.toml, in its order and with its words."""
    sections: list[Section] = []
    title, about, group = "", "", ""
    comment: list[str] = []
    options = ""
    for line in BLANK_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        text = line.removeprefix("#").removeprefix(" ")
        if not line.strip():
            comment, options = [], ""
        elif line.startswith("# ---"):
            continue
        elif m := re.match(r"^# ([A-Z][A-Z. ]+)(?:: (.+))?$", line):
            name = m[1].strip()
            title, about = name if len(name) <= 3 else name.capitalize(), m[2] or ""
        elif m := re.match(r"^\[(\w+)\]$", line):
            sections.append(Section(m[1], title, about))
            group = ""
        elif m := re.match(r"^# -- (.+) --$", line):
            group = m[1]
        elif m := _OPTION.match(line):
            sections[-1].options.append(
                Option(
                    sections[-1].name, m[1], group, _help(comment), _choices(options)
                )
            )
            comment = []  # the words are said once, of options that share them
        elif text.startswith("Options:"):
            options = text.removeprefix("Options:")
        elif options:
            options += " " + text.strip()  # the list goes on on the next line
        elif line.startswith("#"):
            comment.append(text)
    return sections


# Options most people never change: the page folds them away.
ADVANCED = frozenset(
    {
        "downloads.requests_per_minute",
        "downloads.verify_ssl",
        "tidal.client_id",
        "tidal.client_secret",
        "tidal.api_redirect_uri",
        "spotify.device_name",
        "spotify.redirect_uri",
        "database.downloads_path",
        "database.failed_downloads_path",
        "filepaths.truncate_to",
        "cli.max_search_results",
    }
)


def _kind(option: Option, value) -> str:
    """What the page shows for an option of this value: a switch, a number, ..."""
    match value:
        case bool():
            return "bool"
        case int():
            return "int"
        case list():
            return "list"
    return "secret" if "secret" in option.name else "text"


def _checked(option: Option, value, current):
    """`value` for `option`, if it is of the config's own type and, where the
    template lists them, one of its values."""
    kind = _kind(option, current)
    fits = {
        "bool": lambda: isinstance(value, bool),
        "int": lambda: isinstance(value, int) and not isinstance(value, bool),
        "list": lambda: (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ),
    }.get(kind, lambda: isinstance(value, str))()
    if not fits or (option.choices and value not in [v for v, _ in option.choices]):
        raise ProviderError(f"{option.label}: {value!r} is not one of its values.")
    return value.strip() if isinstance(value, str) else value


def _settings(**extra) -> Response:
    """Every option with its words and its value, for the page."""
    config = get_config()
    return jsonify(
        **extra,
        path=str(config.path),
        sections=[
            {
                "name": s.name,
                "title": s.title,
                "about": s.about,
                "options": [
                    {
                        "name": o.name,
                        "label": o.label,
                        "group": o.group,
                        "help": o.help,
                        "choices": o.choices,
                        "kind": _kind(
                            o, value := getattr(getattr(config.file, s.name), o.name)
                        ),
                        "advanced": f"{s.name}.{o.name}" in ADVANCED,
                        "value": value,
                    }
                    for o in s.options
                ],
            }
            for s in layout()
            if s.options
        ],
    )


@settings_pages.route("")
@settings_pages.route("/file")
def page() -> Response:
    return spa()


@settings_pages.get("/api")
def get_settings() -> Response:
    return _settings()


@settings_pages.post("/api")
def save() -> Response:
    """Take the changes the page sent, {section: {name: value}}, and save them."""
    config, sent_changes = get_config(), sent().get("changes")
    if not isinstance(sent_changes, dict):
        raise ProviderError("No changes sent.")
    options = {(s.name, o.name): o for s in layout() for o in s.options}
    changes: dict[str, dict] = {}
    for section, values in sent_changes.items():
        for name, value in (values if isinstance(values, dict) else {}).items():
            if (option := options.get((section, name))) is None:
                raise ProviderError(f"No option {name} in [{section}].")
            current = getattr(getattr(config.file, section), name)
            changes.setdefault(section, {})[name] = _checked(option, value, current)
    if changes:
        config.update_options(changes)
    return _settings()


@settings_pages.post("/reset")
def reset() -> Response:
    """Every option back to its default. The logins stay (they are not in the
    file), and the old file is kept beside it."""
    config = get_config()
    config.save_file()  # what this run changed (a token) first
    backup = reset_file(config.path)
    config.reload()
    kept = f" The old file is {backup.name}, next to it." if backup else ""
    return _settings(message=f"Every setting is back to its default.{kept}")


@settings_pages.get("/file/api")
def file_text() -> Response:
    path = get_config().path
    return jsonify(path=str(path), text=path.read_text(encoding="utf-8-sig"))


@settings_pages.post("/file/api")
def save_file() -> Response:
    """The file as written by hand, once streamrip can read it."""
    config, text = get_config(), str(sent().get("text") or "")
    try:
        unused = ConfigData.from_toml(text).unused
    except Exception as e:
        raise ProviderError(f"Not saved: {type(e).__name__}: {e}") from e
    config.save_file()  # what this run changed (a token) first
    write_private(config.path, text)
    config.reload()
    return jsonify(
        message=f"Saved. Not options of this streamrip: {', '.join(unused)}."
        if unused
        else "Saved."
    )
