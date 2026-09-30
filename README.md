![streamrip logo](https://github.com/Stensel8/streamrip/blob/dev/demo/logo.svg?raw=true)

[![CI](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue)](https://github.com/Stensel8/streamrip/blob/dev/pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A fast, all-in-one scriptable music downloader for Qobuz, Deezer, Tidal, and SoundCloud.

> [!NOTE]
> A fork of [nathom/streamrip](https://github.com/nathom/streamrip) that works
> again: Python 3.14+, with most open upstream issues and pull requests worked
> through (see [CHANGELOG.md](CHANGELOG.md)). Streaming services change their
> APIs without notice, so it needs upkeep: it works now, with no promise that
> it stays maintained.

![downloading an album](https://github.com/Stensel8/streamrip/blob/dev/demo/download_album.png?raw=true)

- Tracks, albums, playlists, discographies and labels, several at once
- Spotify and Apple Music playlists through [last.fm](https://www.last.fm)
- Tagged, with cover art, and converted to FLAC, ALAC, AIFF, MP3, AAC, OGG or Opus if you like
- Remembers what it downloaded; `streamrip repair` retries what failed
- Interactive search on every source

## Installation

### Latest release (recommended)

[![Latest release](https://img.shields.io/github/v/release/Stensel8/streamrip?display_name=tag&sort=semver)](https://github.com/Stensel8/streamrip/releases/latest)

Download and extract the [latest release](https://github.com/Stensel8/streamrip/releases/latest),
then open the extracted directory and run the local installer. It fetches
Python 3.14 with [uv](https://docs.astral.sh/uv/) if needed and installs
streamrip into a `.venv` next to the installer.

```bash
cd streamrip-*
./install.sh
```

```powershell
Set-Location streamrip-*
.\install.ps1
```

With Python 3.14 already there, install it into a venv of your own, or with
[pipx](https://pipx.pypa.io/) or uv:

```bash
python3 -m venv .venv && source .venv/bin/activate  # fish: activate.fish
pip install .

pipx install .
uv tool install .
```

### `dev` branch (unstable, active development)

Download and extract the [`dev` branch](https://github.com/Stensel8/streamrip/archive/refs/heads/dev.zip),
then open the extracted directory and run the local installer. This is
unstable and may be broken between commits.

```bash
cd streamrip-dev
./install.sh
```

```powershell
Set-Location streamrip-dev
.\install.ps1
```

```bash
pip install .
```

streamrip needs [ffmpeg](https://ffmpeg.org/download.html) (Tidal hi-res,
SoundCloud, conversion) and checks for it before logging in to anything:
`apt install ffmpeg`, `brew install ffmpeg` or `winget install ffmpeg`, or the
bundled one with `pip install imageio-ffmpeg` in streamrip's environment
(`pipx inject streamrip imageio-ffmpeg` for pipx).

> [!IMPORTANT]
> `pip install streamrip`, the AUR package and `brew install streamrip` install
> **upstream** streamrip, whose last release doesn't install on Python 3.14.

## Logging in

streamrip asks for what it needs the first time you use a source, and saves it
in the config. A login that stops working is asked for again.

- **Qobuz** needs a subscription, or downloads the albums you bought. Qobuz's
  web login has no device-code flow to poll (unlike Tidal's), but it does save
  a working login token for itself the moment you're logged in, so streamrip
  offers a choice of how to grab that:
  1. **An isolated browser window** that logs in and captures the token with
     no further input from you. Drives a Chrome-family browser already on
     your machine (Chrome, Edge, Brave, Chromium, ...) in a throwaway
     profile — no existing session to work around, nothing downloaded. If
     none is found (Firefox/LibreWolf-only setups), it asks before
     downloading Playwright's own small browser instead.
  2. **Your own browser**, open to [the login page](https://play.qobuz.com/login).
     Log in first, *then* paste the short script streamrip prints into the
     console (F12) — Qobuz reloads the page on login, which would stop the
     script if pasted beforehand. It reads the token Qobuz's own web player
     already saves for itself in `localStorage`. Nothing is downloaded or
     driven; a few lines of JavaScript run in the browser you already have
     open.
  3. **By hand**: copy `user.id` and `user_auth_token` from DevTools →
     Network yourself, or use email/password.

  Options 1 and 2 fall back to manual entry if nothing arrives within a few
  minutes. Credentials are saved for later use either way.
- **Tidal** needs a subscription. streamrip shows a link to log in with on any
  device, twice: Tidal serves hi-res (up to 24-bit, 192 kHz) and CD quality
  through two separate logins. Each track is asked for in hi-res first and falls
  back to 16-bit FLAC when Tidal has no hi-res master. `hires_client = false` in
  `[tidal]` skips the hi-res login and gets 16-bit FLAC only.
- **Deezer** needs the `arl` cookie of a logged-in session
  ([how to find it](https://github.com/nathom/streamrip/wiki/Finding-Your-Deezer-ARL-Cookie));
  the quality follows your subscription.
- **SoundCloud** needs nothing.

## Usage

```bash
streamrip url https://www.qobuz.com/us-en/album/rumours-fleetwood-mac/0603497941032  # one or more URLs
streamrip file urls.txt                                      # the URLs in a file
streamrip --codec mp3 url https://tidal.com/browse/album/147569387
streamrip --quality 3 url https://tidal.com/browse/album/147569387
streamrip search tidal album 'fleetwood mac rumours'         # pick from the results
streamrip lastfm https://www.last.fm/user/nathan3895/playlists/12126195
streamrip repair                                             # retry what failed
streamrip config open                                        # every option, explained
```

By default streamrip asks every source for its highest quality and steps down,
one level at a time, when a track doesn't have it. `--quality` is the highest
quality to download (SoundCloud has one per track):

| Quality | Audio                 | Sources              |
| ------- | --------------------- | -------------------- |
| 0       | 128 kbps MP3 or AAC   | Deezer, Tidal        |
| 1       | 320 kbps MP3 or AAC   | Deezer, Tidal, Qobuz |
| 2       | 16 bit, 44.1 kHz (CD) | Deezer, Tidal, Qobuz |
| 3       | 24 bit, ≤ 96 kHz      | Tidal, Qobuz         |
| 4       | 24 bit, ≤ 192 kHz     | Qobuz                |

Every command has a `--help`.

![streamrip interactive search](https://github.com/Stensel8/streamrip/blob/dev/demo/playlist_search.png?raw=true)

## Contributing

Report problems in the [issue tracker](https://github.com/Stensel8/streamrip/issues),
with the Bug Report or Feature Request template. For code, `poetry install --all-extras`
sets things up; `poetry run pytest` and `poetry run ruff check . && poetry run ruff format .`
should pass before you open a pull request to `dev`.

## Acknowledgements

streamrip was written by [nathom](https://github.com/nathom) (consider
[sponsoring nathom](https://github.com/sponsors/nathom)); this fork builds on
everyone who sent fixes upstream, credited in [CHANGELOG.md](CHANGELOG.md).
Thanks to Vitiko98, Sorrow446 and DashLt for their contributions. streamrip was
inspired by [rip](https://github.com/nathom/streamrip),
[qobuz-dl](https://github.com/vitiko98/qobuz-dl),
[Qo-DL Reborn](https://github.com/badumbass/Qo-DL-Reborn),
[Tidal-Media-Downloader](https://github.com/yaronzz/Tidal-Media-Downloader)
and [scdl](https://github.com/flyingrub/scdl).

## Disclaimer

I will not be responsible for how **you** use streamrip. By using streamrip, you
agree to the terms and conditions of the Qobuz, Tidal, and Deezer APIs.
