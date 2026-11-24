"""state.toml: the logins and apps streamrip keeps for itself, next to config.toml.

config.toml is for settings, which people edit. What streamrip earns by itself
(tokens, the app a login belongs to, what it scraped) lives here, so a save of
one never undoes the other, and every program that uses the same folder (the
command line, `streamrip web`) shares the same logins.
"""

import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from tomlkit.api import dumps

logger = logging.getLogger("streamrip")

STATE_NAME = "state.toml"

# The fields of config.toml's sections that are state: Config reads them from
# here, and writes them back here.
CREDENTIALS = frozendict(
    {
        "qobuz": ("user_id", "auth_token", "app_id", "secrets"),
        "tidal": (
            "user_id",
            "country_code",
            "access_token",
            "refresh_token",
            "token_expiry",
            "token_client_id",
            "hires_access_token",
            "hires_refresh_token",
            "hires_token_expiry",
            "hires_token_client_id",
            # The developer app of Tidal's official API, and its login.
            "api_client_id",
            "api_client_secret",
            "api_access_token",
            "api_refresh_token",
            "api_token_expiry",
        ),
        "deezer": ("arl",),
        "soundcloud": ("client_id", "app_version", "oauth_token"),
        "spotify": (
            "client_id",
            "access_token",
            "refresh_token",
            "token_expiry",
            "scope",
            "soloist_api_key",
        ),
    }
)


def blank(field: str):
    """What a state field is while nothing is stored: nothing."""
    return [] if field == "secrets" else ""


def write_private(path: Path, contents: str):
    """Write contents to path in one step, private from its first byte.

    The contents go to a temporary file next to it, which replaces the file only
    once it is complete: a crash, a Ctrl-C or a full disk part way through leaves
    the old file, instead of a truncated one. mkstemp creates the file 0600
    whatever the umask, so there is no moment at which tokens are readable by
    others.
    """
    # A symlinked file (kept in a dotfiles repo, say) is written through, not
    # replaced by a regular file.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{path.name}-", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            if os.name == "posix":
                os.fchmod(f.fileno(), 0o600)
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class State:
    """The state file at `path`. With no path nothing is kept (the packaged
    template of the config has no folder of its own)."""

    def __init__(self, path: Path | None):
        self.path = path

    def read(self) -> dict[str, dict[str, Any]]:
        """{section: {field: value}}; empty when there is no file, or none that
        can be read."""
        if self.path is None:
            return {}
        try:
            with self.path.open("rb") as f:
                return tomllib.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, tomllib.TOMLDecodeError) as e:
            logger.warning("Cannot read the logins in %s: %s", self.path, e)
            return {}

    def update(self, changes: dict[str, dict[str, Any]]):
        """Write `changes` into the file as it is now, leaving what it holds
        besides alone: another streamrip may have saved its own login meanwhile."""
        if self.path is None or not changes:
            return
        state = self.read()
        for section, values in changes.items():
            state.setdefault(section, {}).update(values)
        write_private(self.path, dumps(state))
