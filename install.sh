#!/usr/bin/env bash
# Installs streamrip (this fork) for Linux and macOS in one step, into an
# explicit .venv in the current directory -- never system Python, and not
# a global shim either.
#
# Uses uv (https://docs.astral.sh/uv/) to fetch Python 3.14 if it isn't
# already installed, then creates .venv and installs streamrip into it.
# Safe to re-run: it reuses the existing .venv and upgrades streamrip.
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found, installing it (see https://docs.astral.sh/uv/)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d .venv ]; then
    echo "Creating .venv (Python 3.14)..."
    uv venv --python 3.14 .venv
fi

echo "Installing streamrip into .venv..."
uv pip install --python .venv --upgrade \
    "streamrip[ffmpeg] @ git+https://github.com/Stensel8/streamrip.git"

echo
if .venv/bin/streamrip --version >/dev/null 2>&1; then
    echo "Done. $(.venv/bin/streamrip --version) is installed at .venv/bin/streamrip."
else
    echo "Install finished, but .venv/bin/streamrip --version failed -- something's wrong."
    exit 1
fi
echo
echo "Activate the venv so plain 'streamrip' works, then use it normally:"
echo "  source .venv/bin/activate       # bash/zsh"
echo "  source .venv/bin/activate.fish  # fish"
echo
echo "If 'streamrip' then runs something else, a shell alias or function named"
echo "streamrip is shadowing it (check with 'command -v streamrip', or 'type"
echo "streamrip' in fish). .venv/bin/streamrip always works regardless of"
echo "that, activated or not."
