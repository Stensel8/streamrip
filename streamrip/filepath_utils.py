import re
from string import printable

from pathvalidate import sanitize_filename, sanitize_filepath  # type: ignore

ALLOWED_CHARS = set(printable)

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
    path = str(sanitize_filepath(fn))
    if restrict:
        path = "".join(c for c in path if c in ALLOWED_CHARS)

    # A formatted folder name can exceed the per-name limit even when every
    # field in it was truncated ("File name too long", upstream #856, #859).
    parts = re.split(r"([\\/])", path)
    return "".join(p if p in ("/", "\\") else truncate_str(p) for p in parts)


def fit_filename(stem: str, extension: str) -> str:
    """Build ``stem.extension`` shortened so the whole name fits one component."""
    budget = MAX_COMPONENT_BYTES - len(extension.encode()) - 1
    return f"{truncate_str(stem, budget)}.{extension}"
