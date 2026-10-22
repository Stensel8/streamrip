# Installs streamrip (this fork) for Windows in one step, into an explicit
# .venv in the current directory -- never system Python, and not a global
# shim either.
#
# Uses uv (https://docs.astral.sh/uv/), which fetches Python 3.15 if it isn't
# installed yet and installs streamrip with the versions of uv.lock. Safe to
# re-run: it updates .venv to this checkout (and remakes it if it isn't on
# Python 3.15).

Set-Location $PSScriptRoot
$venvPath = Join-Path $PSScriptRoot ".venv"
$streamripPath = Join-Path $venvPath "Scripts\streamrip.exe"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found, installing it (see https://docs.astral.sh/uv/)..."
    powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "Installing streamrip into .venv..."
$env:UV_PROJECT_ENVIRONMENT = $venvPath
uv sync --locked --no-dev --no-editable --extra browser

Write-Host ""
$version = & $streamripPath --version 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Installed: $version"
    Write-Host "Environment: $venvPath"
    Write-Host "Run directly: $streamripPath"
} else {
    Write-Host "Install finished, but $streamripPath --version failed -- something's wrong."
    exit 1
}
Write-Host ""
Write-Host "Activate the venv so plain 'streamrip' works:"
Write-Host "  & `"$venvPath\Scripts\Activate.ps1`""
Write-Host "Leave it again with 'deactivate'."
Write-Host ""
Write-Host "If 'streamrip' then runs something else, an alias or function named"
Write-Host "streamrip is shadowing it (check with 'Get-Command streamrip -All')."
Write-Host "$streamripPath always works regardless of that, activated or not."
