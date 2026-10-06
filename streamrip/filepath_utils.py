import os
import re
from string import printable

from pathvalidate import sanitize_filename  # type: ignore

ALLOWED_CHARS = set(printable)

# What separates the folders of a path template: both kinds of slash on every
# system, since a template written on Windows has to work elsewhere too.
_SEPARATORS = re.compile(r"[\\/]")

# Most filesystems (ext4, APFS, NTFS) cap a single file or folder name at 255
# bytes. Stay a little below it so suffixes streamrip adds while working
# (".dash.mp4", a converted extension) still fit.
MAX_COMPONENT_BYTES = 240


def truncate_str(text: str, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Truncate to at most ``max_bytes`` of UTF-8 without splitting a character."""
    str_bytes = text.encode()[:max_bytes]
    return str_bytes.decode(errors="ignore").rstrip()


def clean_filename(fn: str, restrict: bool = False) -> str:
    path = truncate_str(str(sanitize_filename(fn)))
    if restrict:
        path = "".join(c for c in path if c in ALLOWED_CHARS)

    return path


def clean_filepath(fn: str, restrict: bool = False) -> str:
    """Clean a relative folder path made from a template and metadata.

    Every component is cleaned like a file name, which also keeps it within
    the per-name limit ("File name too long", upstream #856, #859). None can
    lead out of the folder the path is joined to: empty components (a leading
    or doubled separator, or a field with nothing in it) are dropped and "."
    and ".." become underscores. An artist called ".." is all it takes to turn
    "{albumartist}/{title}" into "../Title".
    """
    parts = []
    for part in _SEPARATORS.split(fn):
        part = clean_filename(part, restrict)
        if not part:
            continue
        if part in (".", ".."):
            part = part.replace(".", "_")
        parts.append(part)
    return os.sep.join(parts) or "Unknown"


def ensure_inside(root: str, path: str) -> str:
    """Return path, or raise ValueError unless it is somewhere below root.

    The paths are compared as written, `..` resolved but symlinks not
    followed: a link the user made in the download folder, to an artist folder
    on a NAS say, is theirs to make. Whatever streaming services send as
    metadata cannot make one.
    """
    root_abs, path_abs = os.path.abspath(root), os.path.abspath(path)
    try:
        inside = (
            path_abs != root_abs
            and os.path.commonpath((root_abs, path_abs)) == root_abs
        )
    except ValueError:  # another drive, on Windows
        inside = False
    if not inside:
        raise ValueError(f"{path!r} is not inside the download folder {root!r}")
    return path


def fit_filename(stem: str, extension: str) -> str:
    """Build ``stem.extension`` shortened so the whole name fits one component."""
    budget = MAX_COMPONENT_BYTES - len(extension.encode()) - 1
    return f"{truncate_str(stem, budget)}.{extension}"
