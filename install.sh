#!/usr/bin/env bash
# Installs streamrip (this fork) for Linux and macOS in one step, into an
# explicit .venv in the current directory -- never system Python, and not
# a global shim either.
#
# Uses uv (https://docs.astral.sh/uv/), which fetches Python 3.15 if it isn't
# installed yet and installs streamrip, with ffmpeg, at the versions of
# uv.lock. Safe to
# re-run: it updates .venv to this checkout (and remakes it if it isn't on
# Python 3.15).
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"
venv_path="$script_dir/.venv"
streamrip_path="$venv_path/bin/streamrip"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found, installing it (see https://docs.astral.sh/uv/)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing streamrip into .venv..."
uv sync --locked --no-dev --no-editable --extra browser --extra ffmpeg

echo
if "$streamrip_path" --version >/dev/null 2>&1; then
    echo "Installed: $("$streamrip_path" --version)"
    echo "Environment: $venv_path"
    echo "Run directly: $streamrip_path"
else
    echo "Install finished, but $streamrip_path --version failed -- something's wrong."
    exit 1
fi
echo
# A script run via `bash` or `curl | bash` executes in a child process, so it
# cannot activate a venv in the shell you're typing into -- that one command
# is unavoidable. $SHELL is your *login* shell, not necessarily the one
# running this, so it's not reliable here (e.g. login shell zsh, actually
# running fish); the parent process is.
parent_shell=$(ps -o comm= -p "$PPID" 2>/dev/null || true)
echo "Activate the venv so plain 'streamrip' works:"
if [ "$parent_shell" = "fish" ]; then
    echo "  source \"$venv_path/bin/activate.fish\""
else
    echo "  source \"$venv_path/bin/activate\"       # bash/zsh"
    echo "  source \"$venv_path/bin/activate.fish\"  # fish"
fi
echo "Leave it again with 'deactivate'."
echo
echo "If 'streamrip' then runs something else, a shell alias or function named"
echo "streamrip is shadowing it (check with 'command -v streamrip', or 'type"
echo "streamrip' in fish). .venv/bin/streamrip always works regardless of"
echo "that, activated or not."
