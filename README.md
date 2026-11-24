![streamrip logo](demo/logo.svg)

[![CI](https://opencommit.eu/Stensel8/streamrip/badges/workflows/ci.yml/badge.svg?branch=dev&label=CI)](https://opencommit.eu/Stensel8/streamrip/actions?workflow=ci.yml)
[![Latest release](https://img.shields.io/gitea/v/release/Stensel8/streamrip?gitea_url=https%3A%2F%2Fopencommit.eu&display_name=tag&sort=semver)](https://opencommit.eu/Stensel8/streamrip/releases/latest)
[![Python 3.15](https://img.shields.io/badge/python-3.15-blue)](https://opencommit.eu/Stensel8/streamrip/src/branch/dev/pyproject.toml)
[![Image size](https://ghcr-badge.egpl.dev/stensel8/streamrip/size)](https://github.com/Stensel8/streamrip/pkgs/container/streamrip)
[![Image vulnerabilities](https://img.shields.io/github/actions/workflow/status/Stensel8/streamrip/container-scan.yml?branch=dev&label=image%20vulnerabilities)](https://github.com/Stensel8/streamrip/actions/workflows/container-scan.yml)
[![Mirror: github.com](https://img.shields.io/badge/mirror-github.com-blue)](https://github.com/Stensel8/streamrip)
[![Mirror: opencommit.eu](https://img.shields.io/badge/mirror-opencommit.eu-blue)](https://opencommit.eu/Stensel8/streamrip)
[![Mirror: codetrunk.eu](https://img.shields.io/badge/mirror-codetrunk.eu-blue)](https://repos.codetrunk.eu/Stensel8/streamrip)

**Keep the music you pay for. Take your playlists with you.**

streamrip downloads from Qobuz, Tidal, Deezer, SoundCloud and Spotify, and moves
your playlists between Spotify, Tidal and CSV. From the command line or in your
browser.

> [!IMPORTANT]
> GitHub has taken repositories down before, so streamrip lives in three places.
> Bookmark them:
> - **[opencommit.eu/Stensel8/streamrip](https://opencommit.eu/Stensel8/streamrip)**
>   is the main one. Development and dependency updates happen there.
> - **[github.com/Stensel8/streamrip](https://github.com/Stensel8/streamrip)**
>   is kept in sync with it.
> - **[repos.codetrunk.eu/Stensel8/streamrip](https://repos.codetrunk.eu/Stensel8/streamrip)**
>   copies GitHub every 4 hours.

> [!NOTE]
> This is a fork of [nathom/streamrip](https://github.com/nathom/streamrip) that
> I keep working and up to date ([what changed](CHANGELOG.md)). It needs the
> latest stable Python. Streaming services change their APIs without warning,
> so things do break now and then. I work on opencommit.eu and check
> [GitHub](https://github.com/Stensel8/streamrip) and
> [codetrunk.eu](https://repos.codetrunk.eu/Stensel8/streamrip) now and then.

> [!CAUTION]
> `pip install streamrip`, the AUR package and `brew install streamrip` all
> install **upstream** streamrip, which doesn't run on Python 3.15.

![downloading an album from Qobuz](demo/download.avif)

- Download tracks, albums, playlists, discographies and labels, and your liked
  tracks and albums on Tidal and Deezer. Spotify links
  ([details](docs/spotify.md)), CSV lists ([details](docs/csv-lists.md)) and
  last.fm playlists work too.
- Search any source from the terminal and pick what you want.
- Files come with tags, cover art and lyrics, and can be converted to FLAC,
  ALAC, AIFF, MP3, AAC, OGG or Opus.
- streamrip remembers what it downloaded, and `streamrip repair` retries
  whatever failed.
- Move your liked songs and playlists between Spotify and Tidal, export them to
  CSV and back, or download them from another source, like a Spotify playlist
  in lossless from Qobuz ([details](docs/playlists.md)). This used to be
  [Music-Sync](https://github.com/Stensel8/Music-Sync).
- `streamrip web` does all of this in your browser, logins included
  ([details](docs/web.md)).

## Why streamrip exists

Most of us pay for music every month and own none of it. Stop paying and the
library is gone. Albums disappear from the catalogue and from your playlists, and
"offline" downloads only play inside the app. A subscription of about €11 a month
costs some €130 a year, the price of seven or eight CDs. After ten years you'd
have a shelf full of CDs. With a subscription you have nothing.

Many services, Qobuz among them, also sell single tracks and albums. If you
bought an album, it's all the more reasonable to want the files.

streamrip turns what you pay for, by subscription or purchase, into files on your
own disk, in the best quality the service has, properly tagged and yours to
keep.

You're also never stuck with one service. The files are FLAC, M4A or MP3 with
standard tags and the lists are CSV, so they work in any player, on a NAS, in
Jellyfin or Plex, in the car, and twenty years from now. `streamrip playlist`
moves your playlists from Spotify to Tidal and back, to CSV or onto your disk. If
a service raises its prices or drops your favourite album, take your music and
playlists elsewhere. In the EU that's even your right
([GDPR article 20](https://gdpr-info.eu/art-20-gdpr/)).

Tools like Soundiiz and TuneMyMusic only move the lists and leave the music
behind. They're closed source, need access to your accounts and often charge once
you have more than a few playlists. streamrip is free software
([AGPL-3.0](#license)) that runs on your own computer.

It's the idea behind [Stop Killing Games](https://en.wikipedia.org/wiki/Stop_Killing_Games)
and [Louis Rossmann](https://en.wikipedia.org/wiki/Louis_Rossmann)'s FULU
Foundation: if you paid for it, you should get to keep it. So use streamrip for
music you pay for, and support the artists you like by buying their records and
going to their shows. See the [Disclaimer](#disclaimer).

Streaming services change their APIs, logins and rules without warning, and every
change can break streamrip. That's why I keep this fork going.

## Install

### With Python

Download **Source code (ZIP)** from the [latest release on GitHub](https://github.com/Stensel8/streamrip/releases/latest)
and unpack it, or run `git clone https://opencommit.eu/Stensel8/streamrip`. Then
run the installer in that folder:

```bash
./install.sh                                              # Linux, macOS
powershell -ExecutionPolicy Bypass -File .\install.ps1    # Windows
```

It uses [uv](https://docs.astral.sh/uv/) to put streamrip and ffmpeg in a
`.venv` in that folder, and installs Python 3.15 if you don't have it. Start
streamrip with `.venv/bin/streamrip` (Windows: `.venv\Scripts\streamrip.exe`),
or activate the venv and type `streamrip`.

### With a wheel

If you already have [uv](https://docs.astral.sh/uv/), download the `.whl` from
the [latest release](https://opencommit.eu/Stensel8/streamrip/releases/latest)
and run:

```bash
uv tool install ./streamrip-*.whl
```

uv fetches Python 3.15 if you need it. The wheel has everything in it: the web
interface, the automatic login browser and a bundled ffmpeg.

### With Podman or Docker

The image has everything built in, ffmpeg included, and runs on amd64 and arm64:

```bash
mkdir -p streamrip
podman run --rm -it -v ./streamrip:/data -v ~/Music:/data/Music \
  opencommit.eu/stensel8/streamrip url <link>
```

Downloads go to `~/Music`, and config and databases to `./streamrip`. The image
is also on `ghcr.io/stensel8/streamrip`. For Docker, SELinux and more, see
[docs/container.md](docs/container.md).

### With Nix

```bash
nix run . -- url <link>      # x86_64 Linux, from this checkout
```

More in [docs/nix.md](docs/nix.md).

## Use

```bash
streamrip url https://tidal.com/browse/album/234793           # an album, artist, playlist or track
streamrip url <link> <link>                                     # several at once
streamrip file urls.txt                                         # the links in a file, one per line
streamrip url <link> --preview                                  # what it would do, without downloading
streamrip id tidal album 234793                                 # by source, type and ID
streamrip search tidal album 'linkin park meteora'              # search and pick
streamrip search --first tidal track 'linkin park in the end'   # search and take the first result
streamrip csv linkin-park.csv                                   # a CSV list of tracks
streamrip lastfm https://www.last.fm/user/nathan3895/playlists/12126195
streamrip --codec mp3 url <link>                                # convert while downloading
streamrip --quality 2 url <link>                                # CD quality at most
streamrip repair                                                # retry what failed
streamrip config open                                           # every option, explained
streamrip web                                                   # all of it in your browser
```

And for playlists ([details](docs/playlists.md)):

```bash
streamrip playlist transfer spotify tidal --liked               # liked songs, Spotify to Tidal
streamrip playlist download spotify --playlist "Road trip" --source qobuz   # in lossless
streamrip playlist export spotify --all -o backup/              # every list as CSV
streamrip playlist import tidal list.csv --playlist "From CSV"  # a CSV into a playlist
```

Links from Qobuz, Deezer, SoundCloud and Spotify work the same way. So do links
to your likes, like `https://listen.tidal.com/my-collection/tracks` (or
`/albums`) and a Deezer profile's `/loved` or `/albums`. Every command has
`--help`.

streamrip asks each source for its best quality and steps down when a track
isn't available in it. `--quality` sets the highest it asks for:

| Quality | Audio                                        | Sources                   |
| ------- | -------------------------------------------- | ------------------------- |
| 0       | 128 kbps MP3 (Deezer), 256 kbps AAC (Tidal)  | Deezer, Tidal, Spotify    |
| 1       | 320 kbps MP3 or AAC                          | Deezer, Tidal, Qobuz, Spotify |
| 2       | 16 bit, 44.1 kHz (CD)                        | Deezer, Tidal, Qobuz, Spotify |
| 3       | 24 bit, ≤ 96 kHz (Qobuz), ≤ 192 kHz (Tidal)  | Tidal, Qobuz              |
| 4       | 24 bit, ≤ 192 kHz                            | Qobuz                     |

![searching Deezer and Qobuz, with the cover and details of each result](demo/search.avif)

The search menu shows the cover of each result. Terminals that can display
images show the real thing: Ghostty, Kitty, WezTerm, Konsole and foot on Linux,
Windows Terminal, iTerm2 on macOS, and VS Code with
`terminal.integrated.enableImages`. Other terminals get colored blocks. This
also works in the container and over SSH.

## Logging in

streamrip asks for what it needs the first time you use a source and saves it in
the config. If a login stops working, it asks again.

| Source | What you need |
| --- | --- |
| Qobuz | A subscription, or albums you bought. Log in through an isolated browser window, your own browser, or by hand. |
| Tidal | A subscription. You log in with a link on any device (twice for hi-res). |
| Deezer | The `arl` cookie of a logged-in session, from an isolated browser window or by hand. |
| SoundCloud | Nothing: it works as a guest. A login (the `oauth_token` cookie) gives better quality and higher limits. |
| Spotify | Your own Spotify app, and Premium on the account that owns it. Lossless, up to 44.1 kHz. The track plays in full and is recorded, so it rips slower than the other sources, and where Spotify will not play a track it falls back to a 320 kbps match on YouTube Music. The playlist tools use the same app and login. |

For your Tidal playlists, the playlist tools also need a Tidal developer app
([how](docs/playlists.md#tidal)). More per source in [Logging in](docs/logging-in.md)
and [Spotify](docs/spotify.md). You can also do all of this on the Accounts page
of `streamrip web`.

## More

- [Playlists](docs/playlists.md): transfer, export, import and download your playlists and liked songs
- [Web interface](docs/web.md): what it does, and why it should only run on your own machine
- [Spotify](docs/spotify.md): setting up the app, what works, and the quality you get
- [CSV lists](docs/csv-lists.md): download a list from streamrip, Exportify or a spreadsheet
- [Lyrics](docs/lyrics.md): where they come from and how to turn the lookup off
- [Podman and Docker](docs/container.md): the image, its volumes and building it yourself
- [Nix](docs/nix.md): the flake and its development shell

## Contributing

Report problems in the [issue tracker on opencommit.eu](https://opencommit.eu/Stensel8/streamrip/issues)
or on [GitHub](https://github.com/Stensel8/streamrip/issues).

For code, `uv sync` sets everything up. Make sure `uv run pytest`
and `uv run ruff check . && uv run ruff format .` pass before you open a pull
request against `dev`. Tests that need a real browser are skipped when there
isn't one. CI skips the login and session tests. [checks/](checks/README.md)
lets you run those yourself, along with the real-account checks and everything
CI does.

## Acknowledgements

streamrip was written by [nathom](https://github.com/nathom) (consider
[sponsoring nathom](https://github.com/sponsors/nathom)). Thanks to all the
contributors, upstream and in this fork. The fixes from upstream are credited in
[CHANGELOG.md](CHANGELOG.md).

streamrip was inspired by [rip](https://github.com/nathom/streamrip),
[qobuz-dl](https://github.com/vitiko98/qobuz-dl),
[Qo-DL Reborn](https://github.com/badumbass/Qo-DL-Reborn),
[Tidal-Media-Downloader](https://github.com/yaronzz/Tidal-Media-Downloader)
and [scdl](https://github.com/flyingrub/scdl). Lyrics come from
[LRCLIB](https://lrclib.net). The Spotify support borrows from three projects
([credits](docs/spotify.md#credits)). The playlist tools and the web interface
come from Music-Sync, which grew out of csv2tidal by Nugman and borrows
from spotify_to_tidal ([credits](docs/playlists.md#credits)).

## License

streamrip is free software under the [GNU Affero General Public License,
version 3 only](LICENSE) (AGPL-3.0-only). It grew out of two projects:

- **[streamrip](https://github.com/nathom/streamrip)** by nathom and its
  contributors, under the [GNU General Public License, version 3
  only](LICENSES/GPL-3.0-only.txt). The code that comes from there
  stays available under that licence as well.
- **[Music-Sync](https://github.com/Stensel8/Music-Sync)** by Stensel8,
  under the AGPL-3.0-only. It began as
  [csv2tidal](https://github.com/Nugman/csv2tidal) by Nugman, under the
  AGPL-3.0. The playlist tools (`streamrip/sync`, `streamrip playlist`) and the
  web interface come from it.

Section 13 of both licences allows combining a work under one with a work under
the other and conveying the result as one. streamrip is conveyed under the
AGPL-3.0, whose section 13 (offering the source to everyone who uses it over a
network, which `streamrip web` makes possible) applies to the whole. Every part
keeps the licence it came with. The licence texts are unchanged copies of those
of the Free Software Foundation, as their own terms require.

Copyright © nathom and the contributors of streamrip; Stensel8 and the
contributors of this fork; Nugman, Stensel8 and the contributors of Music-Sync.

## Disclaimer

I work on streamrip out of goodwill and share it for free. It comes as is, without
any warranty (see the [licence](LICENSE)).

By using streamrip, you confirm that you know what you're doing and accept
responsibility for it. You agree to the terms and conditions of the Qobuz, Tidal,
Deezer and Spotify APIs, and of YouTube Music, for the Spotify tracks whose audio
it serves. Only download music you've paid for or are otherwise allowed to keep. I'm
not responsible for how **you** use streamrip.
