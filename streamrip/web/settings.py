"""The Settings page: every option of config.toml, laid out and explained as the
template does, and the file itself to edit by hand."""

import re
from dataclasses import dataclass, field
from functools import cache

from flask import Blueprint, flash, redirect, render_template, request, url_for
lazy from werkzeug.wrappers import Response as WerkzeugResponse

from ..config import BLANK_CONFIG_PATH, ConfigData, write_private
from .common import get_config

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
    """Comment lines as text: wrapped lines joined, indented ones (a list) kept."""
    text = ""
    for line in lines:
        indented = line.startswith("  ")
        text += ("\n" if indented or not text else " ") + line.strip()
    return text.strip()


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


def _value(option: Option, current):
    """What the form sent for `option`, as the type the config has it. A switch
    that is off sends nothing; any other option left out stays as it is."""
    key = f"{option.section}.{option.name}"
    if isinstance(current, bool):
        return request.form.get(key) == "on"
    if (sent := request.form.get(key)) is None:
        return current
    if isinstance(current, int):
        try:
            return int(sent)
        except ValueError:
            raise ValueError(f"{option.label}: a number, not {sent!r}") from None
    if isinstance(current, list):
        return [item.strip() for item in sent.split(",") if item.strip()]
    return sent.strip()


@settings_pages.route("")
def page() -> str:
    config = get_config().file
    values = {
        (s.name, o.name): getattr(getattr(config, s.name), o.name)
        for s in layout()
        for o in s.options
    }
    return render_template("settings.html", sections=layout(), values=values)


@settings_pages.post("")
def save() -> WerkzeugResponse:
    """Take every option the form sent that differs from the config."""
    config = get_config()
    changes: dict[str, dict] = {}
    try:
        for section in layout():
            current = getattr(config.file, section.name)
            for option in section.options:
                value = _value(option, getattr(current, option.name))
                if value != getattr(current, option.name):
                    changes.setdefault(section.name, {})[option.name] = value
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("settings.page"))
    if changes:
        config.update_options(changes)
    count = sum(map(len, changes.values()))
    flash(f"Saved {count} change{'s' * (count != 1)}." if count else "Nothing changed.")
    return redirect(url_for("settings.page"))


@settings_pages.route("/file")
def file_page() -> str:
    path = get_config().path
    return render_template(
        "settings_file.html", path=path, text=path.read_text(encoding="utf-8-sig")
    )


@settings_pages.post("/file")
def save_file() -> WerkzeugResponse | tuple[str, int]:
    """The file as written by hand, once streamrip can read it."""
    config, text = get_config(), request.form.get("text", "")
    try:
        unused = ConfigData.from_toml(text).unused
    except Exception as e:
        message = f"Not saved: {type(e).__name__}: {e}"
        return render_template(
            "settings_file.html", path=config.path, text=text, error=message
        ), 400
    config.save_file()  # what this run changed (a token) first
    write_private(config.path, text)
    config.reload()
    if unused:
        flash(f"Saved. Not options of this streamrip: {', '.join(unused)}.", "error")
    else:
        flash("Saved.")
    return redirect(url_for("settings.page"))
