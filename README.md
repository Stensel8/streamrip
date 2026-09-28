![streamrip logo](https://github.com/Stensel8/streamrip/blob/dev/demo/logo.svg?raw=true)

[![CI](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue)](https://github.com/Stensel8/streamrip/blob/dev/pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A scriptable music downloader for Qobuz, Tidal, Deezer and SoundCloud.

> [!NOTE]
> A fork of [nathom/streamrip](https://github.com/nathom/streamrip) that works
> again: Python 3.14+, with most open upstream issues and pull requests worked
> through (see [CHANGELOG.md](CHANGELOG.md)). Streaming services change their
> APIs without notice, so it needs upkeep to keep working.

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

Add `@<branch-or-tag>` to the URL for a specific version. Conversion and Tidal
hi-res downloads need [ffmpeg](https://ffmpeg.org/download.html)
(`apt install ffmpeg`, `brew install ffmpeg`).

> [!IMPORTANT]
> `pip install streamrip`, the AUR package and `brew install streamrip` install
> **upstream** streamrip, whose last release doesn't install on Python 3.14.

## Logging in

streamrip asks for what it needs the first time you use a source, and saves it
in the config. A login that stops working is asked for again.

- **Qobuz** needs a subscription, or downloads the albums you bought. Its login
  page has a captcha, so streamrip offers to open a browser and take the token
  from your login there. Without a desktop, enter your user id and
  `user_auth_token` yourself: log in at [play.qobuz.com](https://play.qobuz.com/login)
  and copy both from the `user/login` response in DevTools → Network.
- **Tidal** needs a subscription. streamrip shows a link to log in with on any
  device. It gets 16-bit FLAC for every lossless release; `hires_client = true`
  in `[tidal]` gets 24-bit instead, and AAC for releases that aren't hi-res.
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

`--quality` is the highest quality to download (SoundCloud has one per track):

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
Thanks to Vitiko98, Sorrow446 and DashLt, and to the projects streamrip grew
from: [qobuz-dl](https://github.com/vitiko98/qobuz-dl),
[Qo-DL Reborn](https://github.com/badumbass/Qo-DL-Reborn),
[Tidal-Media-Downloader](https://github.com/yaronzz/Tidal-Media-Downloader)
and [scdl](https://github.com/flyingrub/scdl).

## Disclaimer

I will not be responsible for how **you** use streamrip. By using streamrip, you
agree to the terms and conditions of the Qobuz, Tidal, and Deezer APIs.
