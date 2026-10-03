"""The traceback handler `streamrip -v` installs must not print secrets."""

import sys

import pytest
from click.testing import CliRunner

from streamrip.console import console
from streamrip.rip.cli import rip

FAKE_ARL = "f00dfeedc0ffee" * 8


@pytest.fixture(autouse=True)
def _restore_excepthook():
    """rip installs rich's handler process-wide; put the original back."""
    saved = sys.excepthook
    yield
    sys.excepthook = saved


def _login(arl):
    """Stand-in for DeezerClient.login: fails with the ARL in a local."""
    raise RuntimeError("Deezer login failed")


@pytest.mark.parametrize("verbose", [True, False])
def test_traceback_does_not_print_local_variables(tmp_path, verbose):
    """A traceback is what gets pasted into bug reports.

    Under -v, rich's handler rendered every frame's locals, so a failed login
    printed `arl = '<the ARL>'`, and any frame holding a Config printed it
    again through its repr.
    """
    args = ["--config-path", str(tmp_path / "config.toml"), "config", "path"]
    if verbose:
        args.insert(0, "-v")
    result = CliRunner().invoke(rip, args)
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
