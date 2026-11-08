import os
import re
from pathlib import Path
from string import printable

ALLOWED_CHARS = frozenset(printable)

# What no file name may hold on Linux, macOS or Windows: control characters and
# the ones Windows reserves.
_INVALID_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
# Windows' reserved device names, in any case and with any extension.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "CLOCK$", "NUL"}
    | {f"{d}{n}" for d in ("COM", "LPT") for n in (*"0123456789", "¹", "²", "³")}
)

# What separates the folders of a path template: both kinds of slash on every
# system, since a template written on Windows has to work elsewhere too.
_SEPARATORS = re.compile(r"[\\/]")

# Most filesystems (ext4, APFS, NTFS) cap a single file or folder name at 255
# bytes. Stay a little below it so suffixes streamrip adds while working
# (".part.mp4", a converted extension) still fit.
MAX_COMPONENT_BYTES = 240


def truncate_str(text: str, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Truncate to at most ``max_bytes`` of UTF-8 without splitting a character."""
    str_bytes = text.encode()[:max_bytes]
    return str_bytes.decode(errors="ignore").rstrip()


def sanitize_filename(name: str) -> str:
    """A file name that is valid on Linux, macOS and Windows alike.

    Exactly what pathvalidate 3's sanitize_filename made of it, which streamrip
    used before, so the names of an existing library stay the same: invalid
    characters go, the name is cut at 255 bytes, and then one problem left is
    fixed: a reserved name gets an underscore, or else spaces and dots go from
    the end and spaces from the start.
    """
    if not name.strip():
        return ""
    name = _INVALID_CHARS.sub("", name).encode()[:255].decode(errors="ignore")
    if not name.strip():
        return ""
    if name in {".", ".."}:
        return name
    root = name if name.startswith("...") else re.match(r"[^.]*", name)[0]
    if root.upper() in _RESERVED_NAMES or name.upper() in _RESERVED_NAMES:
        return name.replace(root, f"{root}_")
    if name[-1] in " ." or name[0] == " ":
        name = name.strip(" ")
        return name if name in {".", ".."} else name.rstrip(" .")
    return name


def clean_filename(fn: str, restrict: bool = False) -> str:
    path = truncate_str(sanitize_filename(fn))
    if restrict:
        path = "".join(c for c in path if c in ALLOWED_CHARS)

    return path


def clean_filepath(fn: str, restrict: bool = False) -> Path:
    """Clean a relative folder path made from a template and metadata.

    Every component is cleaned like a file name, which also keeps it within
    the per-name limit ("File name too long", upstream #856, #859). None can
    lead out of the folder the path is joined to: empty components (a leading
    or doubled separator, or a field with nothing in it) are dropped and "."
    and ".." become underscores. An artist called ".." is all it takes to turn
    "{albumartist}/{title}" into "../Title".
    """
    parts = []
    for raw in _SEPARATORS.split(fn):
        part = clean_filename(raw, restrict)
        if not part:
            continue
        if part in {".", ".."}:
            part = part.replace(".", "_")
        parts.append(part)
    return Path(*parts) if parts else Path("Unknown")


def ensure_inside(root: Path, path: Path) -> Path:
    """Return path, or raise ValueError unless it is somewhere below root.

    The paths are compared as written, `..` resolved but symlinks not
    followed: a link the user made in the download folder, to an artist folder
    on a NAS say, is theirs to make. Whatever streaming services send as
    metadata cannot make one.
    """
    # Not Path.resolve(), which would follow symlinks.
    root_abs = Path(os.path.abspath(root))  # noqa: PTH100
    path_abs = Path(os.path.abspath(path))  # noqa: PTH100
    if path_abs == root_abs or not path_abs.is_relative_to(root_abs):
        raise ValueError(f"'{path}' is not inside the download folder '{root}'")
    return path


def fit_filename(stem: str, extension: str) -> str:
    """Build ``stem.extension`` shortened so the whole name fits one component.

    A stem that is no name (empty, as a title of only characters a file name
    can't hold leaves it, or "." or "..") becomes "Unknown": ".flac" or
    "..flac" would be a hidden file without an extension.
    """
    budget = MAX_COMPONENT_BYTES - len(extension.encode()) - 1
    stem = truncate_str(stem, budget)
    if stem in {"", ".", ".."}:
        stem = "Unknown"
    return f"{stem}.{extension}"
