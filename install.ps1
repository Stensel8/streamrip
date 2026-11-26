# Installs streamrip (this fork) for Windows in one step, into an explicit
# .venv in the current directory -- never system Python, and not a global
# shim either.
#
# Uses uv (https://docs.astral.sh/uv/), which fetches Python 3.15 if it isn't
# installed yet and installs streamrip, with ffmpeg, at the versions of
# uv.lock. Safe to
# re-run: it updates .venv to this checkout (and remakes it if it isn't on
# Python 3.15).

Set-Location $PSScriptRoot
$venvPath = Join-Path $PSScriptRoot ".venv"
$streamripPath = Join-Path $venvPath "Scripts\streamrip.exe"

# uv's installer: its release, and the sha256 it must have (Patchbot keeps both
# current). A changed file is not run.
$UV_INSTALLER = "0.13.10@sha256:5b8e12d392c80cd6ccf0f758b71fa82fe6f5e0dead5871dc94e96afc44876269"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found, installing it (see https://docs.astral.sh/uv/)..."
    $version, $sha = $UV_INSTALLER -split "@sha256:"
    $installer = Join-Path ([IO.Path]::GetTempPath()) "uv-installer.ps1"
    Invoke-WebRequest -UseBasicParsing -OutFile $installer -Uri "https://github.com/astral-sh/uv/releases/download/$version/uv-installer.ps1"
    if ((Get-FileHash -Algorithm SHA256 $installer).Hash -ne $sha) {
        Remove-Item $installer
        Write-Error "The uv installer is not the one expected; not running it."
        exit 1
    }
    powershell -ExecutionPolicy ByPass -File $installer
    Remove-Item $installer
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "Installing streamrip into .venv..."
uv sync --locked --no-dev --no-editable
if ($LASTEXITCODE) { exit $LASTEXITCODE }

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
