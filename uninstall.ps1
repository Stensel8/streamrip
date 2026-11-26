# Removes streamrip and what it keeps on this computer (Windows): the program,
# its config, logins and databases. Your music stays where it is, and so do uv
# and Python. Asks first; -Yes removes streamrip's own without asking (and
# leaves what others share).
param([switch]$Yes)

function Ask($question) {
    (Read-Host "$question [y/N]") -match '^[yY]'
}

$config = Join-Path $env:APPDATA "streamrip"
$music = $null
if (Test-Path (Join-Path $config "config.toml")) {
    $line = Select-String -Path (Join-Path $config "config.toml") -Pattern '^folder = "(.*)"$' | Select-Object -First 1
    if ($line) { $music = $line.Matches[0].Groups[1].Value }
}

$paths = @(
    (Join-Path $PSScriptRoot ".venv"),
    $config,
    (Join-Path $env:LOCALAPPDATA "streamrip")
) | Where-Object { Test-Path $_ }
$tools = @()
if ((Get-Command uv -ErrorAction SilentlyContinue) -and (uv tool list 2>$null | Select-String '^streamrip ')) { $tools += "uv" }
if ((Get-Command pipx -ErrorAction SilentlyContinue) -and (pipx list --short 2>$null | Select-String '^streamrip ')) { $tools += "pipx" }

if (-not $paths -and -not $tools) {
    Write-Host "streamrip has nothing on this computer."
} else {
    Write-Host "This removes:"
    $paths | ForEach-Object { Write-Host "  $_" }
    $tools | ForEach-Object { Write-Host "  streamrip, installed with $_" }
    if ($Yes -or (Ask "Go ahead?")) {
        if ($tools -contains "uv") { uv tool uninstall streamrip | Out-Null }
        if ($tools -contains "pipx") { pipx uninstall streamrip | Out-Null }
        $paths | ForEach-Object { Remove-Item -Recurse -Force -LiteralPath $_ }
        Write-Host "Removed."
    }
}

# Shared with other programs: only when asked, never with -Yes.
if (-not $Yes) {
    $browser = Join-Path $env:LOCALAPPDATA "ms-playwright"
    if ((Test-Path $browser) -and (Ask "Also remove Playwright's browsers in $browser (other programs may use them)?")) {
        Remove-Item -Recurse -Force -LiteralPath $browser
    }
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $images = docker images --format '{{.Repository}}:{{.Tag}}' 2>$null | Where-Object { $_ -match '/streamrip:' }
        if ($images -and (Ask "Also remove these docker images: $($images -join ' ')?")) {
            docker rmi $images | Out-Null
        }
    }
}

$where = if ($music) { $music } else { "your Music folder" }
Write-Host "Your music is still in $where; uv and Python stay too."
Write-Host "This folder ($PSScriptRoot) you can delete yourself."
