![streamrip logo](demo/logo.svg)

[![CI](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Stensel8/streamrip?display_name=tag&sort=semver)](https://github.com/Stensel8/streamrip/releases/latest)
[![Python 3.15](https://img.shields.io/badge/python-3.15-blue)](https://github.com/Stensel8/streamrip/blob/dev/pyproject.toml)
[![Image size](https://ghcr-badge.egpl.dev/stensel8/streamrip/size)](https://github.com/Stensel8/streamrip/pkgs/container/streamrip)
[![Image vulnerabilities](https://img.shields.io/github/actions/workflow/status/Stensel8/streamrip/container-scan.yml?branch=dev&label=image%20vulnerabilities)](https://github.com/Stensel8/streamrip/actions/workflows/container-scan.yml)

A fast, all-in-one scriptable music downloader for Qobuz, Deezer, Tidal, SoundCloud and Spotify.

> [!NOTE]
> A fork of [nathom/streamrip](https://github.com/nathom/streamrip) that I keep
> working and up to date (see [CHANGELOG.md](CHANGELOG.md)). It needs the latest
> stable Python. Streaming services change their APIs all the time, so anything
> that talks to them can break without notice.

![downloading an album](demo/download_album.svg)

- Tracks, albums, playlists, discographies and labels
- Spotify links, with the audio found on YouTube Music ([details](docs/spotify.md))
- CSV lists of tracks ([details](docs/csv-lists.md)) and last.fm playlists
- Interactive search on every source
- Tagged, with cover art and lyrics, and converted to FLAC, ALAC, AIFF, MP3, AAC, OGG or Opus if you like
- Remembers what it downloaded; `streamrip repair` retries what failed

## Run it

Pick one. The streamrip commands are the same in all three (see [Use it](#use-it)).

### With Python

Download **Source code (zip)** from the [latest release](https://github.com/Stensel8/streamrip/releases/latest)
and extract it. (Not the `.tar.gz` under Assets: it lacks the installer. For the
unstable development version, take the [`dev` zip](https://github.com/Stensel8/streamrip/archive/refs/heads/dev.zip).)
In the extracted folder:

```bash
./install.sh        # Linux, macOS
.\install.ps1       # Windows (PowerShell)
```

It fetches Python 3.15 with [uv](https://docs.astral.sh/uv/) if you don't have
it, and installs streamrip into a `.venv` next to the installer. Then:

```bash
.venv/bin/streamrip url <link>            # Linux, macOS
.venv\Scripts\streamrip.exe url <link>    # Windows
```

Or activate the venv once (`source .venv/bin/activate`, fish: `activate.fish`;
Windows: `.venv\Scripts\Activate.ps1`) and type `streamrip url <link>`.

Already have Python 3.15? `pip install ".[browser]"` in a venv of your own, or
`pipx install ".[browser]"` / `uv tool install ".[browser]"`. The `browser` part
adds the isolated browser window of the Qobuz and Deezer logins, which needs a
screen: leave it out on a server.

> [!IMPORTANT]
> `pip install streamrip`, the AUR package and `brew install streamrip` install
> **upstream** streamrip, which doesn't work on Python 3.15.

streamrip also needs [ffmpeg](https://ffmpeg.org/download.html) (Tidal hi-res,
SoundCloud, conversion): `apt install ffmpeg`, `brew install ffmpeg` or
`winget install ffmpeg`, or `pip install imageio-ffmpeg` into streamrip's
environment (`pipx inject streamrip imageio-ffmpeg` for pipx).

### With Podman or Docker

The image has everything built in: ffmpeg, and the JavaScript runtime Spotify
needs. It runs on amd64 and arm64. Tags: `latest` and every version, like `2.6.0`.
It is scanned for known vulnerabilities on every change and every week (see the badge).
It does not check for updates, as a container is updated by pulling the newest
image: `podman pull ghcr.io/stensel8/streamrip`.

```bash
mkdir -p streamrip      # the folder must exist
podman run --rm -it -v ./streamrip:/data ghcr.io/stensel8/streamrip url <link>
```

Everything after the image name is a streamrip command, exactly as above.
Config, databases and downloads all live in `/data`, hence the folder; `-it` is
for the prompts, progress bars and search menu.

- **Docker:** the same, with `--user "$(id -u):$(id -g)"` after `run`, so the
  files are yours.
- **SELinux** (Fedora, RHEL): add `:Z` to the volume: `-v ./streamrip:/data:Z`.

To type it like the Python version:

```bash
alias streamrip='podman run --rm -it -v "$HOME/streamrip:/data" ghcr.io/stensel8/streamrip'
```

Build the image yourself with `podman build -t streamrip .` (it reads the
`Containerfile`; Docker: `docker build -f Containerfile -t streamrip .`).

A container has no screen, so it leaves out the isolated browser window of the
Qobuz and Deezer logins and offers the other ways. For Spotify, paste the
address ([Logging in](docs/logging-in.md)).

### With Nix

On x86_64 Linux, from this checkout:

```bash
nix run "path:$PWD" -- url <link>
```

More in [docs/nix.md](docs/nix.md).

## Use it

Every example downloads Linkin Park from Tidal. Links from Qobuz, Deezer,
SoundCloud and Spotify work the same way.

An artist, with their whole discography (`[artist_filters]` in the config says
what to leave out):

```bash
streamrip url https://tidal.com/browse/artist/14123
```

An album:

```bash
streamrip url https://tidal.com/browse/album/234793
```

A track:

```bash
streamrip url https://tidal.com/browse/track/1225577
```

Several links at once:

```bash
streamrip url https://tidal.com/browse/album/1225569 https://tidal.com/browse/album/234793 https://tidal.com/browse/track/1225577
```

The links in a file, one per line:

```bash
streamrip file urls.txt
```

An item by its ID, without a link (source, type, id):

```bash
streamrip id tidal album 234793
```

Search, and pick from the results:

```bash
streamrip search tidal album 'linkin park meteora'
```

Search, and take the first result without asking:

```bash
streamrip search --first tidal track 'linkin park in the end'
```

A CSV list of tracks ([more](docs/csv-lists.md)):

```bash
streamrip csv linkin-park.csv
```

```csv
title,artists
In the End,Linkin Park
Numb,Linkin Park
```

A last.fm playlist:

```bash
streamrip lastfm https://www.last.fm/user/nathan3895/playlists/12126195
```

Convert while downloading (FLAC, ALAC, AIFF, MP3, AAC, OGG or OPUS):

```bash
streamrip --codec mp3 url https://tidal.com/browse/album/234793
```

Stop at a quality (see below):

```bash
streamrip --quality 2 url https://tidal.com/browse/album/234793
```

Retry what failed:

```bash
streamrip repair
```

Every option, explained, in your config:

```bash
streamrip config open
```

Every command has `--help`.

By default every source is asked for its highest quality, stepping down one
level when a track doesn't have it. `--quality` sets the highest to ask for:

| Quality | Audio                 | Sources              |
| ------- | --------------------- | -------------------- |
| 0       | 128 kbps MP3 or AAC   | Deezer, Tidal        |
| 1       | 320 kbps MP3 or AAC   | Deezer, Tidal, Qobuz |
| 2       | 16 bit, 44.1 kHz (CD) | Deezer, Tidal, Qobuz |
| 3       | 24 bit, ≤ 96 kHz      | Tidal, Qobuz         |
| 4       | 24 bit, ≤ 192 kHz     | Qobuz                |

![streamrip interactive search](demo/playlist_search.svg)

Search shows each result's cover next to its details: as an image in terminals
with SIXEL or Kitty graphics (Konsole, foot, Windows Terminal), as colored
blocks elsewhere.

## Logging in

streamrip asks for what it needs the first time you use a source, and saves it
in the config. A login that stops working is asked for again.

| Source | You need |
| --- | --- |
| Qobuz | A subscription, or the albums you bought. Log in with an isolated browser window, your own browser, or by hand. |
| Tidal | A subscription. Log in with a link on any device (twice, for hi-res). |
| Deezer | The `arl` cookie of a logged-in session: an isolated browser window, or by hand. |
| SoundCloud | Nothing. |
| Spotify | An app of your own, and Premium on the account that owns it. The audio comes from YouTube Music and is not lossless. |

Details per source: [Logging in](docs/logging-in.md), [Spotify](docs/spotify.md).

## More

- [Spotify](docs/spotify.md): the app you need, what works, and why it isn't lossless
- [CSV lists](docs/csv-lists.md): download a list exported from Music-Sync, Exportify or a spreadsheet
- [Lyrics](docs/lyrics.md): where they come from, and how to turn the lookup off
- [Nix](docs/nix.md): the flake and its development shell

## Contributing

Report problems in the [issue tracker](https://github.com/Stensel8/streamrip/issues)
with the Bug Report or Feature Request template. For code, `poetry install --all-extras`
sets things up; `poetry run pytest` and `poetry run ruff check . && poetry run ruff format .`
should pass before you open a pull request to `dev`. A few tests drive a real
Chrome-family browser; they are skipped when there is none, and CI fails if its
runner has none. `pytest -m "not real_browser"` leaves them out.

## Acknowledgements

streamrip was written by [nathom](https://github.com/nathom) (consider
[sponsoring nathom](https://github.com/sponsors/nathom)); this fork builds on
everyone who sent fixes upstream, credited in [CHANGELOG.md](CHANGELOG.md).
Thanks to Vitiko98, Sorrow446 and DashLt for their contributions. streamrip was
inspired by [rip](https://github.com/nathom/streamrip),
[qobuz-dl](https://github.com/vitiko98/qobuz-dl),
[Qo-DL Reborn](https://github.com/badumbass/Qo-DL-Reborn),
[Tidal-Media-Downloader](https://github.com/yaronzz/Tidal-Media-Downloader)
and [scdl](https://github.com/flyingrub/scdl). The lyrics lookup uses
[LRCLIB](https://lrclib.net). The Spotify support borrows from three projects,
credited in [docs/spotify.md](docs/spotify.md#credits).

## Disclaimer

I will not be responsible for how **you** use streamrip. By using streamrip, you
agree to the terms and conditions of the Qobuz, Tidal, Deezer and Spotify APIs,
and of YouTube, which the audio of Spotify tracks comes from.
