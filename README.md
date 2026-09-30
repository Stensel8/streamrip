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

The install script fetches Python 3.14 (with [uv](https://docs.astral.sh/uv/))
and installs streamrip into a `.venv` in the current directory:

```bash
curl -LsSf https://raw.githubusercontent.com/Stensel8/streamrip/dev/install.sh | bash  # Linux/macOS
```

```powershell
irm https://raw.githubusercontent.com/Stensel8/streamrip/dev/install.ps1 | iex  # Windows
```

With Python 3.14 already there, install it into a venv of your own, or with
[pipx](https://pipx.pypa.io/) or uv:

```bash
python3 -m venv .venv && source .venv/bin/activate  # fish: activate.fish
pip install git+https://github.com/Stensel8/streamrip.git

pipx install git+https://github.com/Stensel8/streamrip.git
uv tool install git+https://github.com/Stensel8/streamrip.git
```

Add `@<branch-or-tag>` to the URL for a specific version.

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

- **Qobuz** needs a subscription, or downloads the albums you bought. streamrip
  opens [the HTTPS login page](https://play.qobuz.com/login) in your system's
  default browser on Linux, Windows, and macOS. Open DevTools → Network before
  logging in, then copy `user.id` and `user_auth_token` from the `user/login`
  response into streamrip's prompts. If already logged in, log out and log in
  again with Network open. The link is also printed for opening manually.
  No browser is downloaded or automated; credentials are saved for later use.
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
