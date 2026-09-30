# Installs streamrip (this fork) for Windows in one step, into an explicit
# .venv in the current directory -- never system Python, and not a global
# shim either.
#
# Uses uv (https://docs.astral.sh/uv/) to fetch Python 3.14 if it isn't
# already installed, then creates .venv and installs streamrip into it.
# Safe to re-run: it reuses the existing .venv and upgrades streamrip from
# this checkout.

Set-Location $PSScriptRoot
$venvPath = Join-Path $PSScriptRoot ".venv"
$streamripPath = Join-Path $venvPath "Scripts\streamrip.exe"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found, installing it (see https://docs.astral.sh/uv/)..."
    powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

if (-not (Test-Path $venvPath)) {
    Write-Host "Creating .venv (Python 3.14)..."
    uv venv --python 3.14 "$venvPath"
}

Write-Host "Installing streamrip into .venv..."
uv pip install --python "$venvPath" --upgrade "$PSScriptRoot"

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
