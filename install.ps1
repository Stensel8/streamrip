# Installs streamrip (this fork) for Windows in one step, into an explicit
# .venv in the current directory -- never system Python, and not a global
# shim either.
#
# Uses uv (https://docs.astral.sh/uv/) to fetch Python 3.14 if it isn't
# already installed, then creates .venv and installs streamrip into it.
# Safe to re-run: it reuses the existing .venv and upgrades streamrip.

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found, installing it (see https://docs.astral.sh/uv/)..."
    powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

if (-not (Test-Path .venv)) {
    Write-Host "Creating .venv (Python 3.14)..."
    uv venv --python 3.14 .venv
}

Write-Host "Installing streamrip into .venv..."
uv pip install --python .venv --upgrade `
    "streamrip[ffmpeg] @ git+https://github.com/Stensel8/streamrip.git"

Write-Host ""
Write-Host "Done. Activate the venv, then run rip:"
Write-Host "  .venv\Scripts\Activate.ps1"
Write-Host "  rip --help"
