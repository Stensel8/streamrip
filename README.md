![streamrip logo](https://github.com/Stensel8/streamrip/blob/dev/demo/logo.svg?raw=true)

[![CI](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue)](https://github.com/Stensel8/streamrip/blob/dev/pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A scriptable stream downloader for Qobuz, Tidal, Deezer and SoundCloud.

> [!NOTE]
> This is a maintained fork of [nathom/streamrip](https://github.com/nathom/streamrip).
> It targets current Python (3.14+) and fixes a large part of the open
> upstream issues and pull requests; see [CHANGELOG.md](CHANGELOG.md) for what
> changed and which upstream issues are addressed.

![downloading an album](https://github.com/Stensel8/streamrip/blob/dev/demo/download_album.png?raw=true)

## Features

- Fast, concurrent downloads powered by `aiohttp`
- Downloads tracks, albums, playlists, discographies, and labels from Qobuz, Tidal, Deezer, and SoundCloud
- Supports downloads of Spotify and Apple Music playlists through [last.fm](https://www.last.fm)
- Automatically converts files to a preferred format
- Has a database that stores the downloaded tracks' IDs so that repeats are avoided, and `rip repair` to retry the ones that failed
- Resumes interrupted downloads and retries with backoff
- Concurrency and rate limiting
- Interactive search for all sources
- Highly customizable through the config file

## Installation

First, ensure [Python](https://www.python.org/downloads/) 3.14 or newer and
[pip](https://pip.pypa.io/en/stable/installing/) are installed. Then install
[`ffmpeg`](https://ffmpeg.org/download.html). You may choose not to install it,
but conversion and Tidal hi-res downloads need it.

Install streamrip from this repository:

```bash
pip3 install --upgrade git+https://github.com/Stensel8/streamrip.git
```

or, to keep it separate from other Python packages, with
[pipx](https://pipx.pypa.io/) or [uv](https://docs.astral.sh/uv/):

```bash
pipx install git+https://github.com/Stensel8/streamrip.git
uv tool install git+https://github.com/Stensel8/streamrip.git
```

or in a plain virtual environment, which also works if you don't have pipx or
uv and want `rip` and its dependencies kept out of your system Python:

```bash
# bash/zsh
python3 -m venv .venv
source .venv/bin/activate
```

```fish
# fish
python3 -m venv .venv
source .venv/bin/activate.fish
```

then, with the venv active (your prompt shows `(.venv)`), install into it the
same way as above:

```bash
pip install --upgrade git+https://github.com/Stensel8/streamrip.git
```

`rip` is on `PATH` for as long as that venv is active. Leave it with
`deactivate`; come back to it later from the project directory with the same
`source` command, no need to recreate it.

To install a specific branch or release, add `@<branch-or-tag>` to the URL, for example
`git+https://github.com/Stensel8/streamrip.git@dev`.

When you type

```bash
rip
```

it should show the main help page. If you have no idea what these mean, or are having other issues installing, check out the [detailed installation instructions](https://github.com/nathom/streamrip/wiki#detailed-installation-instructions) in the upstream wiki.

> [!IMPORTANT]
> `pip install streamrip` (PyPI), the AUR package and `brew install streamrip`
> all install **upstream** streamrip, not this fork. Upstream's release pins
> `Pillow<11`, which has no wheels for Python 3.14 and fails to build.

### Optional extras

```bash
# Use certifi's CA bundle instead of the system certificates
pip3 install "streamrip[ssl] @ git+https://github.com/Stensel8/streamrip.git"

# Let streamrip capture your Qobuz login token from a real browser
pip3 install "streamrip[qobuz-login] @ git+https://github.com/Stensel8/streamrip.git"
playwright install chromium
```

### Logging in

- **Qobuz** moved its web login behind a captcha, so email/password login no
  longer works for most accounts. When asked, enter your Qobuz **user id** and
  **user_auth_token**: log in at [play.qobuz.com](https://play.qobuz.com/login),
  open your browser's DevTools → Network, find the `user/login` request and
  copy `user.id` and `user_auth_token` from its response. With the
  `qobuz-login` extra installed, streamrip opens a browser and does this for you.
  Accounts without a streaming subscription can download albums they bought.
- **Tidal** logs in through your browser (device login). Tidal decides per app
  which formats it will stream; by default streamrip uses one that gets FLAC
  16/44.1 for every lossless release. Set `hires_client = true` in the `[tidal]`
  section of the config to prefer 24-bit hi-res FLAC instead (ordinary lossless
  releases then come as AAC). No developer account or client id is needed.
- **Deezer** needs the `arl` cookie of a logged-in session, see
  [Finding your Deezer ARL cookie](https://github.com/nathom/streamrip/wiki/Finding-Your-Deezer-ARL-Cookie).
  The download quality is limited to what your subscription allows.
- **SoundCloud** needs no login.

If a saved login stops working, streamrip offers to log in again.

## Example Usage

**For Tidal and Qobuz, you NEED a premium subscription.**

Download an album from Qobuz

```bash
rip url https://www.qobuz.com/us-en/album/rumours-fleetwood-mac/0603497941032
```

Download multiple albums from Qobuz

```bash
rip url https://www.qobuz.com/us-en/album/back-in-black-ac-dc/0886444889841 https://www.qobuz.com/us-en/album/blue-train-john-coltrane/0060253764852
```

Download the album and convert it to `mp3`

```bash
rip --codec mp3 url https://open.qobuz.com/album/0060253780968
```

To set the maximum quality, use the `--quality` option to `0, 1, 2, 3, 4`:

| Quality ID | Audio Quality         | Available Sources                            |
| ---------- | --------------------- | -------------------------------------------- |
| 0          | 128 kbps MP3 or AAC   | Deezer, Tidal, SoundCloud (most of the time) |
| 1          | 320 kbps MP3 or AAC   | Deezer, Tidal, Qobuz, SoundCloud (rarely)    |
| 2          | 16 bit, 44.1 kHz (CD) | Deezer, Tidal, Qobuz, SoundCloud (rarely)    |
| 3          | 24 bit, ≤ 96 kHz      | Tidal (hi-res FLAC), Qobuz, SoundCloud (rarely) |
| 4          | 24 bit, ≤ 192 kHz     | Qobuz                                        |

```bash
rip --quality 3 url https://tidal.com/browse/album/147569387
```

> Using `4` is generally a waste of space. It is impossible for humans to perceive the difference between sampling rates higher than 44.1 kHz. It may be useful if you're processing/slowing down the audio.

Search for playlists matching `rap` on Tidal

```bash
rip search tidal playlist 'rap'
```

![streamrip interactive search](https://github.com/Stensel8/streamrip/blob/dev/demo/playlist_search.png?raw=true)

Search for *Rumours* on Tidal, and download it

```bash
rip search tidal album 'fleetwood mac rumours'
```

Download a last.fm playlist using the lastfm command

```
rip lastfm https://www.last.fm/user/nathan3895/playlists/12126195
```

For more customization, see the config file

```
rip config open
```

If you're confused about anything, see the help pages. The main help pages can be accessed by typing `rip` by itself in the command line. The help pages for each command can be accessed with the `--help` flag. For example, to see the help page for the `url` command, type

```
rip url --help
```

![example_help_page.png](https://github.com/Stensel8/streamrip/blob/dev/demo/example_help_page.png?raw=true)

## Other information

For more in-depth information about `streamrip`, see the help pages and the [upstream wiki](https://github.com/nathom/streamrip/wiki/).

## Contributions

All contributions are appreciated! You can help out the project by opening an issue
or by submitting code.

### Issues

Report problems with this fork in its [issue tracker](https://github.com/Stensel8/streamrip/issues) and
**use the Feature Request or Bug Report templates**, so all the information
needed to debug the issue is there.

### Code

- Fork this repository and clone it
- `poetry install --all-extras` sets up a development environment
- `poetry run pytest` and `poetry run ruff check . && poetry run ruff format .`
  should pass before you commit
- Open a pull request to the `dev` branch

Please document any functions or obscure lines of code. Dependencies are kept up
to date by Renovate; Dependabot handles security updates.

### The Wiki

To help out `streamrip` users that may be having trouble, consider contributing some information to the wiki.
Nothing is too obvious and everything is appreciated.

## Acknowledgements

streamrip was written by [nathom](https://github.com/nathom); this fork builds
on the work of everyone who sent fixes upstream, whose pull requests are
credited in the commit history and [CHANGELOG.md](CHANGELOG.md).

Thanks to Vitiko98, Sorrow446, and DashLt for their contributions to this project, and the previous projects that made this one possible.

`streamrip` was inspired by:

- [qobuz-dl](https://github.com/vitiko98/qobuz-dl)
- [Qo-DL Reborn](https://github.com/badumbass/Qo-DL-Reborn)
- [Tidal-Media-Downloader](https://github.com/yaronzz/Tidal-Media-Downloader)
- [scdl](https://github.com/flyingrub/scdl)

## Disclaimer

I will not be responsible for how **you** use `streamrip`. By using `streamrip`, you agree to the terms and conditions of the Qobuz, Tidal, and Deezer APIs.

## Sponsorship

Consider [sponsoring nathom](https://github.com/sponsors/nathom), the original
author of streamrip, if you enjoy it.
