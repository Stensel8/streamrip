![streamrip logo](https://github.com/Stensel8/streamrip/blob/dev/demo/logo.svg?raw=true)

[![CI](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/Stensel8/streamrip/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Stensel8/streamrip?display_name=tag&sort=semver)](https://github.com/Stensel8/streamrip/releases/latest)
[![Python 3.14 | 3.15](https://img.shields.io/badge/python-3.14%20%7C%203.15-blue)](https://github.com/Stensel8/streamrip/blob/dev/pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A fast, all-in-one scriptable music downloader for Qobuz, Deezer, Tidal, SoundCloud, and Spotify.

> [!NOTE]
> A fork of [nathom/streamrip](https://github.com/nathom/streamrip) that works
> again: Python 3.14 and 3.15, with most open upstream issues and pull requests worked
> through (see [CHANGELOG.md](CHANGELOG.md)). Streaming services change their
> APIs without notice, so it needs upkeep: it works now, with no promise that
> it stays maintained.

![downloading an album](https://github.com/Stensel8/streamrip/blob/dev/demo/download_album.png?raw=true)

- Tracks, albums, playlists, discographies and labels; queue several in one go
- A CSV list of tracks (from [Music-Sync](https://github.com/Stensel8/Music-Sync) or another exporter): streamrip asks where to search it, see [CSV lists](#csv-lists)
- Spotify links (tracks, albums, artists, playlists): Spotify gives the tags and covers, the audio is found on YouTube Music, see [Spotify](#spotify)
- Spotify and Apple Music playlists through [last.fm](https://www.last.fm)
- Tagged, with cover art, and converted to FLAC, ALAC, AIFF, MP3, AAC, OGG or Opus if you like
- Remembers what it downloaded; `streamrip repair` retries what failed
- Interactive search on every source

## Installation

### Latest release (recommended)

[![Latest release](https://img.shields.io/github/v/release/Stensel8/streamrip?display_name=tag&sort=semver)](https://github.com/Stensel8/streamrip/releases/latest)

Open the [latest release](https://github.com/Stensel8/streamrip/releases/latest)
and download **Source code (zip)**. Do not download the `.tar.gz` package from
Assets; it does not include the installer scripts. Extract the source archive,
open its directory, and run the local installer. It fetches Python 3.14 with
[uv](https://docs.astral.sh/uv/) if needed and installs streamrip into a `.venv`
next to the installer.

```bash
./install.sh
```

```powershell
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
./install.sh
```

```powershell
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
     Network yourself.

  Options 1 and 2 fall back to manual entry if nothing arrives within a few
  minutes. Credentials are saved for later use either way.
- **Tidal** needs a subscription. streamrip shows a link to log in with on any
  device, twice: Tidal serves hi-res (up to 24-bit, 192 kHz) and CD quality
  through two separate logins. Each track is asked for in hi-res first and falls
  back to 16-bit FLAC when Tidal has no hi-res master. `hires_client = false` in
  `[tidal]` skips the hi-res login and gets 16-bit FLAC only.
- **Deezer** has no sign-in for outside apps, and its email and password login
  is behind a captcha. What streamrip logs in with is the `arl` cookie of a
  logged-in web session, so it offers a choice of how to get it:
  1. **An isolated browser window**, the same one Qobuz uses (see above). You
     log in there yourself; streamrip keeps only the `arl` cookie once you're
     in, and never sees your password.
  2. **By hand**: copy the `arl` cookie from your browser's DevTools
     ([how to find it](https://github.com/nathom/streamrip/wiki/Finding-Your-Deezer-ARL-Cookie)).

  The quality follows your subscription. **Deezer does not send lyrics**: its
  tracks are tagged without them (Tidal's have them), and streamrip says so when
  you use Deezer. `lyrics = false` in `[downloads]` silences that.
- **SoundCloud** needs nothing.
- **Spotify** needs an app of your own and a login in your browser, once. See
  [Spotify](#spotify) below.

### Spotify

Spotify encrypts its own audio, so streamrip does not download from Spotify.
What it does is what [spotDL](https://github.com/spotDL/spotify-downloader) does:
Spotify says what a track is (title, artists, album, track number, ISRC, cover,
and which tracks an album, artist or playlist has), streamrip finds the same
recording on **YouTube Music** (by its ISRC first, then by artist, title and
length) and downloads that, tagged with what Spotify says. Because of that:

- The audio is what YouTube Music has: AAC of about 128 kbps (`audio_format =
  "m4a"`, kept as it is) or MP3 at `audio_bitrate`. It is **not lossless**, and
  not Spotify's own quality. For lossless, use Qobuz, Tidal or Deezer.
- A track YouTube Music does not have, or has only as another version (live,
  remix, ...), is reported as not found rather than downloaded wrong.
- **For better quality, move the playlist first.** With
  [Music-Sync](https://github.com/Stensel8/Music-Sync) you can move your liked
  songs and playlists from Spotify to Tidal, or export them to a CSV, and then
  download them from a lossless source: [`streamrip csv`](#csv-lists) searches
  such a CSV on Qobuz, Tidal or Deezer. streamrip reminds you of this the first
  time you use Spotify in a run.
- It needs [ffmpeg](https://ffmpeg.org) like every source, and yt-dlp, which
  wants a JavaScript runtime to read YouTube: install [Deno](https://deno.com)
  or [Node.js](https://nodejs.org) if a download fails saying so.

**Spotify's own rules for logging in** (since February 2026): there is no shared
login for outside programs, so you make a small app of your own in Spotify's
developer dashboard, and **the account that owns the app must have Spotify
Premium**. Without Premium Spotify refuses every request. If you have no Premium,
someone who does can make the app and add your Spotify account under *Settings >
User Management*. Making the app takes a minute:

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   log in and choose **Create app**. Name and description can be anything.
2. **Redirect URI**: `http://127.0.0.1:9900/callback`, character for character.
   It has to be `127.0.0.1`, not `localhost`; Spotify no longer accepts that.
3. Under *Which API/SDKs are you planning to use?* tick **Web API** only.

   ![Only Web API is ticked.](https://github.com/Stensel8/Music-Sync/blob/main/docs/images/spotify-web-api-ticked.avif?raw=true)

4. Save, open the app's *Settings* and copy its **Client ID**. You do not need
   the client secret.

Then run streamrip with any Spotify link. The first time it asks for the Client
ID and opens your browser to log in; it catches the answer itself on
`127.0.0.1:9900` (if streamrip runs on another machine, it offers to let you paste
the address you end up on instead). The login is saved in the `[spotify]`
section of the config and renews itself. If you had to give the Client ID by
hand, it goes in `client_id` there.

![What you see without Premium: "Your application is blocked from accessing the Web API since you do not have a Spotify Premium subscription."](https://github.com/Stensel8/Music-Sync/blob/main/docs/images/spotify-premium-required.avif?raw=true)

(Without Premium the dashboard shows this banner, and every request fails with
HTTP 403.)

```bash
streamrip url https://open.spotify.com/album/<album id>
streamrip url https://open.spotify.com/playlist/<id of a playlist of yours> spotify:track:<track id>
streamrip search spotify track 'daft punk get lucky'
```

What Spotify allows an app of this kind, and so what works:

- **Tracks, albums, artists and search** work for anyone's. Search gives at most
  50 results.
- **Playlists** only if you **own them or collaborate on them**: Spotify blocks
  the tracks of a playlist you merely follow, and Spotify's own editorial ones.
  Copy such a playlist into one of your own first.
- Podcasts and local files in a playlist are skipped.
- An app can be used by at most five people (the ones you add under *User
  Management*). That is Spotify's limit for apps in development mode.

All options are in the `[spotify]` section of `streamrip config open`:
`audio_format` (`m4a` or `mp3`), `audio_bitrate` (only when audio has to be
re-encoded), `match_videos` (accept an official video when YouTube Music has no
matching "song"; turn it off to be strict) and `redirect_uri`.

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
quality to download (SoundCloud and Spotify have one):

| Quality | Audio                 | Sources              |
| ------- | --------------------- | -------------------- |
| 0       | 128 kbps MP3 or AAC   | Deezer, Tidal        |
| 1       | 320 kbps MP3 or AAC   | Deezer, Tidal, Qobuz |
| 2       | 16 bit, 44.1 kHz (CD) | Deezer, Tidal, Qobuz |
| 3       | 24 bit, ≤ 96 kHz      | Tidal, Qobuz         |
| 4       | 24 bit, ≤ 192 kHz     | Qobuz                |

Every command has a `--help`.

![streamrip interactive search](https://github.com/Stensel8/streamrip/blob/dev/demo/playlist_search.png?raw=true)

Search shows each result's cover next to its details. Terminals with SIXEL or
Kitty graphics support (Konsole, foot and Windows Terminal, for example) show it
as an image; the others fall back to colored blocks, which are less sharp.

## CSV lists

`streamrip csv list.csv` downloads the tracks of a CSV list by searching them on
a source. It asks **where to search and download them from** (Qobuz, Tidal,
Deezer, Spotify or SoundCloud, with what each gives); `--source tidal` skips the
question, which scripts need, and `--fallback-source qobuz` tries a second
source for the tracks the first does not have.

```bash
streamrip csv playlist.csv
streamrip csv playlist.csv --source tidal --fallback-source qobuz
```

The list comes from anywhere: `music-sync export spotify --liked -o liked.csv`
([Music-Sync](https://github.com/Stensel8/Music-Sync)), Exportify, TuneMyMusic or
a spreadsheet of your own.

- One track per row, with a title and an artist (several artists separated by
  `;`), and if you have them an album and a length (`duration_ms`), which make a
  match surer. Columns are found by their names: those of Music-Sync (`title`,
  `artists`, `album`, `duration_ms`) and of other exporters (`Track Name`,
  `Artist Name(s)`, `Album Name`, `Duration (ms)`, ...). A file without a header
  row is read as `artist,title`. The file has to be UTF-8; commas, semicolons
  (Excel in many countries) and tabs all work as separators.
- The first five results of a search are scored on title, artist and length, as
  Music-Sync does, and one is taken from a score of 0.8. A live version, a remix
  or another artist's cover is not taken for the track: it is reported as not
  found, with the track's name, instead.
- The tracks go in a folder named after the file, like a last.fm playlist, and
  the `[metadata]` options for playlists apply.
- Every track is a search, paced by `requests_per_minute` in `[downloads]`, so a
  long list takes a while.

## Contributing

Report problems in the [issue tracker](https://github.com/Stensel8/streamrip/issues),
with the Bug Report or Feature Request template. For code, `poetry install --all-extras`
sets things up; `poetry run pytest` and `poetry run ruff check . && poetry run ruff format .`
should pass before you open a pull request to `dev`. A few tests drive a real
Chrome-family browser (Chrome, Edge, Chromium, Brave...); they are skipped when
there is none, and CI fails if its runner has none. `pytest -m "not real_browser"`
leaves them out.

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
agree to the terms and conditions of the Qobuz, Tidal, Deezer and Spotify APIs, and of
YouTube, which the audio of Spotify tracks comes from.

## Credits for Spotify support

The Spotify support in this fork borrows from three projects. Thank you:

- **[Music-Sync](https://github.com/Stensel8/Music-Sync)** (AGPL-3.0, the same
  author). The Spotify login and the handling of Spotify's API follow it: OAuth
  with PKCE and a loopback redirect (`streamrip/rip/spotify_login.py`,
  `streamrip/client/spotify.py`), token refresh, and the rules for apps in
  development mode since February 2026 (Premium for the app's owner, ten
  search results per request, playlist tracks under `item`, only your own
  playlists). The matching of a Spotify track with a YouTube Music result
  (`streamrip/client/audio_match.py`) is adapted from its `matching.py`, and the
  screenshots above are from its README.
- **[spotDL](https://github.com/spotDL/spotify-downloader)** (MIT). The idea of
  taking only the metadata from Spotify and the audio from YouTube Music, and of
  looking a track up by its ISRC first and then scoring candidates by name,
  artist, album and length (`streamrip/client/ytmusic.py`); also the loopback
  redirect on `127.0.0.1` for the login.
- **[MediaHarbor](https://github.com/MediaHarbor/mediaharbor)** (GPL-3.0). Read as
  a reference for how an app talks to Spotify's Web API (client credentials and
  user tokens, following `next` links to the end of a list, backing off when
  Spotify answers 429); no code is taken from it. Its way of downloading
  Spotify's own audio, by decrypting the Widevine-protected streams with a
  device file, is **not** used: streamrip does not decrypt Spotify's audio.
