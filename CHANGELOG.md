# Changelog

All notable changes in this fork of [nathom/streamrip](https://github.com/nathom/streamrip). Numbers can refer to
upstream issues and pull requests.

## 2.4.11 (unreleased)

### Spotify

- **Spotify links can be downloaded**: tracks, albums, artists and playlists, as
  `open.spotify.com` links or `spotify:` URIs, and `streamrip search spotify
  track|album|artist|playlist ...`. Spotify encrypts its own audio, so streamrip
  does what [spotDL](https://github.com/spotDL/spotify-downloader) does: Spotify
  supplies the track list, tags (title, artists, album, track and disc numbers,
  date, copyright, ISRC) and cover, and the audio is the same recording on
  YouTube Music, found by ISRC and then by artist, title and length, and
  downloaded with yt-dlp. It is AAC (kept as it is) or MP3, never lossless, and a
  track YouTube Music only has as another version (live, remix, ...) is reported
  as not found. See the new Spotify section of the README, which also lists what
  Spotify allows an app.
- **You make a Spotify app of your own, and log in once.** Since February 2026
  Spotify's API is only open to apps whose owner has Premium, with a client id
  and no secret: streamrip asks for the Client ID the first time, explains where
  to make the app (the redirect URI, ticking Web API), opens your browser to log
  in (OAuth with PKCE) and catches the answer on `127.0.0.1:9900`, or lets you
  paste the address where the browser ends up, on a server without a browser. The
  login renews itself and is saved in the new `[spotify]` section.
- Playlists are limited by Spotify to the ones you own or collaborate on. Other
  playlists, and podcasts and local files, are skipped, with a message that says
  why. Searching is capped at 50 results (Spotify gives ten per request).
- The new `[spotify]` options: `audio_format` (`m4a` or `mp3`), `audio_bitrate`
  (for audio that has to be re-encoded), `match_videos` (accept an official music
  video when YouTube Music has no matching song) and `redirect_uri`.
- A config from before this release has no `[spotify]` section: it is added from
  the template, and written the next time the config is saved. No `config reset`.
- New dependencies: `ytmusicapi` and `yt-dlp[default]`. yt-dlp wants a
  JavaScript runtime (Deno or Node.js) for YouTube; a download that fails saying
  so is explained as such.
- Credits are at the bottom of the README: the login and API handling and the
  matching are those of [Music-Sync](https://github.com/Stensel8/Music-Sync), the
  idea of metadata from Spotify and audio from YouTube Music is spotDL's, and
  [MediaHarbor](https://github.com/MediaHarbor/mediaharbor) was read as a
  reference. Its decryption of Spotify's Widevine-protected audio is not used.

### CSV lists

- **`streamrip csv list.csv` downloads the tracks of a CSV list**, found by
  searching a source, which it asks for ("Where should I search and download them
  from?", with what each source gives) unless `--source` says. `--fallback-source`
  gives a second source for the tracks the first does not have. The columns of
  [Music-Sync](https://github.com/Stensel8/Music-Sync) and of Exportify,
  TuneMyMusic and the like are understood, a file without a header row is read
  as `artist,title`, and commas, semicolons and tabs work as separators. The first
  five results of each search are scored on title, artist and length, as
  Music-Sync does, so a live version, a remix or another artist's cover is not
  taken for the track. The tracks go in a folder named after the file, as a
  playlist.

### Notices

- The first time Spotify is used in a run, streamrip says its audio comes from
  YouTube Music and is lossy, and recommends moving the playlist first with
  [Music-Sync](https://github.com/Stensel8/Music-Sync) (to Tidal, or to a CSV for
  `streamrip csv`), with a link.
- The first time Deezer is used with `lyrics` on, streamrip says that Deezer does
  not send lyrics, so its tracks are tagged without them. The README and the
  comment in the config say so too. Deezer's answer that it has no lyrics for a
  track is no longer a warning for every track; a lyrics request that really
  fails still is.

### Tests

- `find_ffmpeg` is cached, and `test_ffmpeg_utils` left a fake path, or none, in
  the cache for the tests after it. They are forgotten after each test now.

## 2.4.10

### Security

- An album folder can no longer end up outside the downloads folder. Names from
  a streaming service decided where it went: a field that came out empty in a
  folder format like `{albumartist}/{title}` gave `/Title`, a folder in the root
  of the filesystem, and an artist or uploader called `..` (SoundCloud names are
  free text) gave `../Title`. Empty parts of a folder path are dropped now,
  `.` and `..` become underscores, both kinds of slash separate folders, and a
  folder that is not below the downloads folder is refused.

### Downloading

- SoundCloud tracks are skipped when already downloaded. They were downloaded
  again on every run.
- The downloads database keeps ids per source (`tidal_2430924980`, not
  `2430924980`). A track was skipped as downloaded as soon as one of another
  source with the same id had been, which gets likelier the more you download.
  Entries from before hold the bare id, cannot be told apart and still count for
  every source, so nothing is downloaded again after updating;
  `streamrip database clear downloads` forgets them.
  `streamrip database browse downloads` shows the source.
- The failed downloads database is unique per source too. A failure of one
  source kept another's with the same id out of it, and `streamrip repair`
  removed both. An existing database is converted on first use.

### Configuration

- Saving the config is atomic. A crash, Ctrl-C or full disk could leave an empty
  file with your logins gone. A symlinked config stays a symlink.
- `no_update_check = true` under `[cli]` turns the update check off. It is
  commented out in the config by default.

### Command line

- A config that does not load ends the command with exit code 1, not 0.
- The update notice says its command needs git.

### Tests

- The browser login tests are skipped without a Chrome-family browser, and
  fail on CI, instead of waiting for an answer on stdin.
  `pytest -m "not real_browser"` leaves them out.

## 2.4.9

Thanks to [@berettavexee](https://github.com/berettavexee) for the resume check,
the SoundCloud fallback, the Deezer playlist fix and the traceback fix below.

### Search

- One menu for every OS. The separate Linux/macOS and Windows menus
  (`simple-term-menu`, `pick`, `windows-curses`) are replaced by a single
  [Textual](https://textual.textualize.io) menu that works the same everywhere.
  Filter the results with `/`, mark several with `SPACE`; `ENTER` downloads the
  marked results and the highlighted one. The mouse works too.
- The selected result's cover shows beside its details. Terminals with SIXEL or
  Kitty graphics support (Konsole, foot and Windows Terminal, for example) get
  the image, the others colored sextant blocks, which are less sharp. Detection
  is automatic, and the choice is logged with `-v`. Covers are fetched on demand
  with a small cache, and moving on cancels the pending request. A source's
  larger artwork is tried first, its smaller sizes are the fallbacks.

### Downloading

- A resumed download is checked against the server's `Content-Range`. A server
  answering from another offset, a remainder that does not add up to the
  announced total, a close-delimited answer cut short, or a 416 taken as
  "already complete" whatever the partial file's size, used to end as a corrupt
  file. Now the partial file is removed and the track fails with
  `IncompleteDownloadError`, so the next attempt starts from scratch instead of
  resuming onto the same mismatch
  ([PR #28](https://github.com/Stensel8/streamrip/pull/28), by
  [@berettavexee](https://github.com/berettavexee)).

### Deezer

- A geoblocked or delisted playlist track, which Deezer serves from another
  release, is tagged from the track that is served. It kept the requested
  track's metadata before, so the file got the album, title and cover of a
  release it does not come from. For such releases Deezer has no cover, only a
  grey placeholder, and that is what ended up embedded. The cover is fetched
  after the track is resolved, so only the right one is downloaded
  ([PR #36](https://github.com/Stensel8/streamrip/pull/36), by
  [@berettavexee](https://github.com/berettavexee)).

### SoundCloud

- A track marked downloadable no longer fails outright. Its original now comes
  back as a 401 with an empty body when asked anonymously, which crashed with a
  `ContentTypeError`. The track's MP3 stream is used instead, and a track
  without one is reported as not streamable instead of failing an assertion
  ([PR #34](https://github.com/Stensel8/streamrip/pull/34), by
  [@berettavexee](https://github.com/berettavexee)).

### Command line

- `-v` tracebacks no longer print local variables. They included the Deezer
  `arl` after a failed login and every config, credentials and all, and a
  traceback is exactly what people paste into an issue. The stack itself is
  unchanged
  ([PR #35](https://github.com/Stensel8/streamrip/pull/35), by
  [@berettavexee](https://github.com/berettavexee)).

### Python

- streamrip supports Python 3.14 and 3.15, not newer: `requires-python` is now
  `>=3.14,<3.16`. CI runs the tests and the install check on both.

## 2.4.8

Thanks to [@berettavexee](https://github.com/berettavexee) for the Deezer quality fix and the FLAC cover fix below.

### Deezer

- Logging in no longer means digging the `arl` cookie out of DevTools. streamrip
  offers a choice: an isolated browser window where you log in yourself and
  streamrip keeps only the `arl` cookie and never sees your password, or entering it by
  hand, with the steps printed. Entering it by hand is the default, because the
  browser option may have to download a browser first. Deezer has no sign-in for
  outside apps, and its email and password login is behind a captcha.
- The quality comes from asking Deezer, not from `FILESIZE_*`. Those fields are
  often 0 for a format Deezer does serve, which made streamrip download such
  tracks as MP3 320, or skip them ("Missing download info"). Each quality is
  requested from the wanted one down and the first that returns a URL is used;
  the file extension comes from the CDN URL. A quality Deezer lists a size for
  but gives no URL for (a rate limit, say) fails the track instead of silently
  falling back to a lower one
  ([PR #26](https://github.com/Stensel8/streamrip/pull/26), by
  [@berettavexee](https://github.com/berettavexee), with a follow-up for the
  rate-limit case).
- Album folders and labels name the quality the tracks come in. They always said
  `[FLAC] [16B-44.1kHz]`, so `-q 1` gave a FLAC-named folder full of MP3s. It is
  now your configured quality within what your subscription allows, lowered to
  the best quality every track of the album has: one track without FLAC makes it
  an MP3 album. Such a folder can still hold a FLAC, since every track is asked
  for the best quality it has.
- Lossy albums leave the empty `[UnknownB-UnknownkHz]` out of the folder name.
  This also applies to Tidal AAC and SoundCloud.

### Tagging

- Qobuz writes several track artists as separate `ARTIST` values, main artists
  first and then the featured ones, in the same order as Tidal. Qobuz lists
  credits in no fixed order, so the clean and the explicit edition of a track
  used to tag different artists.
- Several album artists are written as separate `ALBUMARTIST` values for every
  source, instead of one `A, B, C` string. Libraries that split multi-valued
  tags (Music Assistant reads them through ffprobe) showed a release credited to
  several artists as one extra artist. Folder names still use the joined string.
- Tagging a FLAC a second time after conversion (FLAC to FLAC) no longer embeds
  the cover twice ([PR #27](https://github.com/Stensel8/streamrip/pull/27), by
  [@berettavexee](https://github.com/berettavexee)).

### Tidal

- Singles and playlist tracks are tagged with their album's own metadata. A
  track response only names its album, so a feature credit ended up in the album
  artist, the track count was missing and the stream date stood in for the
  release date. The album is fetched once per album; if that request fails, the
  old behaviour is the fallback.
- The device login waits for the link's real lifetime (5 minutes, not the
  "10 minutes" it announced), says so when the link expired, and a real
  rejection includes Tidal's own description.

### SoundCloud

- Small playlists, which come with every track's metadata up front, no longer
  fail every track with "not enough values to unpack".

### Search

- The preview in `streamrip search` shows the same details for every source, next
  to the cover: a track's album, number, release date, length, genre, label,
  composer, BPM, key and quality, and likewise for albums, artists and
  playlists. Covers are drawn with quadrant blocks, twice the detail across of
  half blocks.

### Logging in and progress

- Qobuz and Deezer log in through the same browser helper: the same window, the
  same 5-minute wait (Qobuz's was 2 minutes) and the same "Credentials saved to
  config file" message.
- A download whose size is unknown gets a pulsing bar. A total of 0 made it look
  finished: a full bar without a spinner.

### Config

- `[misc] check_for_updates` is gone: the update check runs before every command
  by design and never read it. Config files with a `[misc]` section still load,
  and so do ones without.

### Internals

- Cleanups: `Downloadable` is a plain base class, the media types are one
  table, the redundant album semaphore is gone, playlists hold track ids instead
  of full metadata, and the progress bars, Tidal client and SoundCloud helpers
  share their code. The live Qobuz tests run again (set `QOBUZ_USER_ID` and
  `QOBUZ_AUTH_TOKEN`).
- `streamrip.metadata` no longer exports `AlbumSummary`, `ArtistSummary`,
  `TrackSummary`, `PlaylistSummary` and `LabelSummary`; one `Summary` replaces
  them.

## 2.4.7

### Downloading several artists

- Items download one after another: a selected artist's (or label's) whole
  discography finishes before the next one starts, one album at a time. This also helps to keep rate-limits under control.
  Several artists used to run at once, which mixed their tracks together on
  screen and ran into the rate limit until everything stalled.
- A progress bar per artist or label shows how far through its discography
  the run is (`32% • 12/37 albums`), and the screen is cleared when the next
  artist starts. Track rows show whose track they are.
- Every album gets a `Finished <artist> - <album> [quality]` line when done.
  Albums of three tracks or fewer (singles, EPs) used to get no line at all.
- The summary at the end counts tracks, downloaded, already downloaded and
  failed, instead of "1 item(s) downloaded" for a whole discography, and
  points to `streamrip repair` when something failed. The empty
  "Downloading" line no longer stays behind after the run.

### Qobuz

- Artist discographies leave out releases by other artists now.
- A release listed twice (a clean and an explicit copy, or two quality
  tiers) downloads once, as Tidal already did.
- Email/password login is gone; Qobuz stopped accepting it long ago. In the
  config, `use_auth_token`, `email_or_userid` and `password_or_token` are
  replaced by `user_id` and `auth_token`. Existing users should delete their config file.

### Network

- `requests_per_minute` spaces requests evenly over time, instead of sending them all at once. This helps to avoid hitting rate limits.
- Qobuz and SoundCloud requests now retry connection errors, timeouts, 5xx
  and rate limits (429) the way Tidal's already did. A single timeout used
  to cost a Qobuz album or track. SoundCloud now honours
  `requests_per_minute` at all.
- Cover art downloads retry network errors, so a brief DNS or connection
  failure no longer leaves an album without artwork. This is in line with the retry behavior for the track and album downloads that we already did.
- Every request uses the same, current user agent (Chrome 155), including
  Deezer's API calls, which still sent Chrome 79's before this change.

## 2.4.6

- Update notices show which environment will be upgraded and use its active
  Python (or `uv`) for the suggested command. A failed update check no longer
  prevents the CLI from starting.
- Install scripts install from the local source archive and report the full
  paths to the environment and executable. The README clarifies which release
  archive includes those scripts.

## 2.4.5

- Qobuz login now offers a choice: an isolated browser that logs in and
  captures the token automatically, pasting a short script into your own
  browser's console, or entering it by hand. Falls back to manual entry if
  the automatic options time out.
- Qobuz search accepts the result limit loaded from the TOML config, fixing
  the crash when searching without `--num-results`.
- Tidal hi-res (DASH) downloads no longer leak MP4 container metadata
  (`major_brand`, ffmpeg's `encoder` stamp, ...) into the FLAC's tags.
- Removed the config version/migration system: an incompatible config now
  just says to run `streamrip config reset`, instead of being silently
  rewritten through a growing pile of per-option migration cases.
- `max_connections` (tracks downloaded at once) defaults to 5, down from 6.
- A fully successful download now reports completion too, not just failures.
- The update check now runs, with a spinner, before every download command
  (`url`, `file`, `search`, `lastfm`, `id`) instead of only `url`. The
  suggested upgrade command pins the detected release instead of installing
  whatever `dev` currently has.

## 2.4.4

- ffmpeg is required and checked before any login. Without it streamrip stops
  with install instructions and exit status 1, instead of failing every
  hi-res Tidal track one by one.

## 2.4.3

Security hardening.

- Config files and the app directory are owner-only (`0600`/`0700`).
- Playlist folders stay inside the download folder, and artwork cleanup no
  longer deletes existing files.
- Invalid disc numbers are rejected before any file is written.
- Qobuz search and Last.fm playlist pagination are bounded (Last.fm: max
  10,000 tracks) and fetched one page at a time.
- Qobuz artist URLs are fetched over HTTPS without account credentials.
  Redirects are now an error: use a URL that contains the artist id.

## 2.4.2

- Albums and playlists download one at a time, with a spinner and status
  line while a large one is still resolving. Track progress no longer mixes
  two releases together, each track/album shows its format and quality
  (`[FLAC 24B-96kHz]`), and the doubled-up, corrupted terminal output during
  a long run is fixed.
- Tidal: an album it lists more than once under one artist -- a clean/explicit
  pair, two quality tiers, or a "(Deluxe)"/"[Deluxe]" spelling of the same
  edition -- downloads only its best copy instead of every copy.
- Tidal: requests held back together by a rate limit no longer all retry in
  the same instant and immediately re-trip it.

## 2.4.1

- `[metadata] prefer_explicit` is on by default: when an album or playlist
  lists both a clean and an explicit copy of the same track, only the
  explicit one is downloaded. Costs no extra API calls, only a later start
  (every track is resolved before any of them download, instead of as each
  one resolves). Set it to `false` in an existing config to keep both.

## 2.4.0

### Installation and dependencies

- Targets current Python (3.14+) only; every dependency is on a current
  release with wheels for 3.14. Pillow was capped below 11, which has no 3.14
  wheels and failed to build ([#1032](https://github.com/nathom/streamrip/issues/1032), [#953](https://github.com/nathom/streamrip/issues/953), [#904](https://github.com/nathom/streamrip/issues/904)).
- `pyproject.toml` uses PEP 621 metadata (Poetry 2).
- `appdirs` and `aiodns` are no longer dependencies. `aiodns` forced Windows onto
  an event loop that cannot run ffmpeg, so conversions failed there ([#729](https://github.com/nathom/streamrip/issues/729)).
- HTTP(S)_PROXY / ALL_PROXY environment variables are honoured ([#961](https://github.com/nathom/streamrip/issues/961)).
  Every request goes through one kind of session, so `--no-ssl-verify` now
  also covers the Qobuz app id lookup (it was ignored there), and last.fm
  and that lookup time out on a stalled connection like the rest. A
  certificate error explains the options in every command, not only in
  `url` and `file`.
- Install from GitHub; the update notice points at this fork instead of telling
  you to `pip install streamrip`, which would install upstream.
- The CLI command is `streamrip`, not `rip`: short names like `rip` collide
  with other tools and local shell aliases/functions too easily.

### Qobuz

- Albums download again after Qobuz stopped returning track lists from
  `album/get` and `playlist/get` ([#1012](https://github.com/nathom/streamrip/issues/1012), [PR #1013](https://github.com/nathom/streamrip/pull/1013)).
- Token login: the prompt asks for a user id and `user_auth_token` because the
  password login is behind a captcha now ([#954](https://github.com/nathom/streamrip/issues/954), [#956](https://github.com/nathom/streamrip/issues/956), [#899](https://github.com/nathom/streamrip/issues/899),
  [#854](https://github.com/nathom/streamrip/issues/854)), or streamrip opens a browser and takes them from your login
  there ([PR #955](https://github.com/nathom/streamrip/pull/955)).
- A stale app id/secret is re-fetched automatically ([PR #1031](https://github.com/nathom/streamrip/pull/1031)); failed logins
  close their session ([PR #1005](https://github.com/nathom/streamrip/pull/1005)); credentials are kept out of logs and errors
  ([PR #1037](https://github.com/nathom/streamrip/pull/1037)).
- API errors (e.g. the intermittent Algolia search failure) are reported or
  retried instead of `AssertionError` ([PR #1037](https://github.com/nathom/streamrip/pull/1037)).
- Free accounts can download albums they purchased ([#673](https://github.com/nathom/streamrip/issues/673), [PR #1007](https://github.com/nathom/streamrip/pull/1007)).
- Playlists longer than 500 tracks are complete ([PR #847](https://github.com/nathom/streamrip/pull/847)).
- The album edition ("Deluxe Edition", "2011 Remaster") is kept in the title,
  tags and folder name, so editions no longer merge ([#902](https://github.com/nathom/streamrip/issues/902), [#867](https://github.com/nathom/streamrip/issues/867),
  [PR #1027](https://github.com/nathom/streamrip/pull/1027)); `{version}` and `{tracktotal}` are available in `folder_format`.
- Single tracks of multi-disc albums go into their `Disc N` folder ([PR #1026](https://github.com/nathom/streamrip/pull/1026)).
- Missing performer/ISRC/genre no longer crash a track ([#668](https://github.com/nathom/streamrip/issues/668)).
- `download_booklets` works: an album's PDF booklets are saved in its folder.
  The option was there (and on) but nothing downloaded them since v1.

### Tidal

- Lossless tracks download as FLAC again instead of AAC 320 ([#966](https://github.com/nathom/streamrip/issues/966),
  [#897](https://github.com/nathom/streamrip/issues/897), [#968](https://github.com/nathom/streamrip/issues/968),
  [PR #1017](https://github.com/nathom/streamrip/pull/1017)), and hi-res tracks
  served as MPEG-DASH are downloaded and remuxed instead of silently downgrading to AAC
  ([#974](https://github.com/nathom/streamrip/issues/974), [PR #998](https://github.com/nathom/streamrip/pull/998)).
- Highest quality first, then one step down at a time. Since late April 2026 no
  single Tidal client is served everything ([#966](https://github.com/nathom/streamrip/issues/966)): the default one gets FLAC
  16/44.1 for every lossless release but never hi-res; the hi-res one gets 24-bit
  FLAC (measured up to 192 kHz) for hi-res releases but only AAC 320 for the rest.
  streamrip logs in with both (`hires_client`, now on by default: a second login,
  asked for once) and asks the hi-res client first for every track Tidal doesn't
  mark as lacking a hi-res master (the `HIRES_LOSSLESS` tag), then the default one.
  A hi-res login from before is moved to its own config fields. It is only used
  with `quality = 3`, so `--quality 2` needs no second login.
- New defaults ask for the best each source has: Tidal `quality = 3`, Qobuz
  `quality = 4` (Deezer was already at FLAC). Existing configs keep their values.
- Folder names of hi-res albums show the real format (`[24B-96kHz]`) instead of
  always `[16B-44.1kHz]`: Tidal's album data only says LOSSLESS, so the format is
  read from the playback info of one track (one extra request per hi-res album).
- Tracks without lyrics are no longer dropped, and lyrics errors never abort a
  track ([#983](https://github.com/nathom/streamrip/issues/983), [#959](https://github.com/nathom/streamrip/issues/959), [#866](https://github.com/nathom/streamrip/issues/866), [PR #1036](https://github.com/nathom/streamrip/pull/1036), [PR #1024](https://github.com/nathom/streamrip/pull/1024)).
- Share links ending in `/u` work ([PR #911](https://github.com/nathom/streamrip/pull/911)).
- Expired or revoked logins offer a fresh login instead of a traceback
  ([#896](https://github.com/nathom/streamrip/issues/896), [#906](https://github.com/nathom/streamrip/issues/906), [#901](https://github.com/nathom/streamrip/issues/901), [PR #1029](https://github.com/nathom/streamrip/pull/1029)); refreshed tokens are saved.
- `null` copyright no longer crashes ([PR #979](https://github.com/nathom/streamrip/pull/979)); unknown quality values
  (`HI_RES_LOSSLESS`) no longer raise `KeyError`.
- Search returned nothing when there was exactly one hit, which broke last.fm
  playlists on Tidal.
- The "requested HI_RES but Tidal only has LOSSLESS" warning no longer appears
  for `quality = 3`, which means "best available": it is only shown when
  lossless was requested and a lossy stream came back. `hires_client` only
  matters together with `quality = 3`.
- API requests retry connection errors, timeouts, 5xx and 429 (up to four
  attempts with backoff) instead of dropping the track. A 429 pauses every
  request and honours `Retry-After`.
- Albums with a `null` copyright no longer fail: the fix for it (upstream PR #979)
  only reached the code for single tracks. Folder names say `FLAC`/`AAC` and
  `44.1kHz` like the other sources, instead of `MP4` and `44100kHz`.
- Music videos in albums and playlists are left out instead of each failing as
  a track (and being retried by `streamrip repair` forever); a playlist with
  videos no longer loses its last tracks when paging.

### Deezer

- The quality is limited to what the subscription allows instead of failing with
  "HiFi is required for quality 2" ([#1015](https://github.com/nathom/streamrip/issues/1015)); `--quality 3/4` no
  longer raises `IndexError`.
- Playlists (including private ones) fall back to the gw API when the public API
  refuses them ([#827](https://github.com/nathom/streamrip/issues/827), [PR #973](https://github.com/nathom/streamrip/pull/973), [PR #945](https://github.com/nathom/streamrip/pull/945)).
- New `link.deezer.com/s/...` share links ([#865](https://github.com/nathom/streamrip/issues/865), [#818](https://github.com/nathom/streamrip/issues/818), [PR #887](https://github.com/nathom/streamrip/pull/887)).
- Redirected album ids are followed ([#893](https://github.com/nathom/streamrip/issues/893)).
- Delisted tracks are recovered through their fallback track; the retired CDN is
  no longer used ([PR #1028](https://github.com/nathom/streamrip/pull/1028)).
- Loved tracks (`/profile/<id>/loved`) can be downloaded ([PR #1008](https://github.com/nathom/streamrip/pull/1008), [PR #987](https://github.com/nathom/streamrip/pull/987)).
- Synced lyrics are embedded; `[downloads] lyrics` turns lyrics off ([PR #984](https://github.com/nathom/streamrip/pull/984)).
- All artists are credited ([#853](https://github.com/nathom/streamrip/issues/853), [PR #994](https://github.com/nathom/streamrip/pull/994), [PR #907](https://github.com/nathom/streamrip/pull/907)); missing fields no
  longer crash parsing ([PR #944](https://github.com/nathom/streamrip/pull/944)).
- Connection pool warnings are gone ([PR #997](https://github.com/nathom/streamrip/pull/997)); album metadata is cached
  ([PR #1000](https://github.com/nathom/streamrip/pull/1000)); blocking calls no longer stall other downloads.
- Tracks you uploaded to Deezer yourself (negative ids, no album) are skipped
  in playlists and loved tracks instead of each failing ([PR #832](https://github.com/nathom/streamrip/pull/832)).
- Encrypted tracks are decrypted as they arrive instead of being held in memory
  whole, and written in large chunks instead of 6 KB at a time; logging in no
  longer fetches the account's profile a second time.

### SoundCloud

- The client id is found again ([#1038](https://github.com/nathom/streamrip/issues/1038)); tracks without an MP3 HLS stream use
  the progressive stream or are skipped instead of crashing.
- HLS segments are reassembled in their original order regardless of which one
  finishes downloading first; concurrent, out-of-order completion produced
  skips and reordered audio ([#848](https://github.com/nathom/streamrip/issues/848), [#633](https://github.com/nathom/streamrip/issues/633)).
- Those segments are removed from the temp dir afterwards (also when one
  fails) instead of piling up, and a failed join shows ffmpeg's error.
- Tracks are tagged disc 1 instead of disc 0.

### Downloads, conversion and tagging

- Failed downloads are no longer marked as downloaded, resolve failures are
  recorded (whatever the error, for single tracks too), and `streamrip repair`
  retries everything in the failed database
  ([PR #1023](https://github.com/nathom/streamrip/pull/1023)). A track skipped
  after a 403 (e.g. a geoblocked Deezer track in an otherwise public playlist)
  no longer leaves the run in a state where cleanup trips over the file it
  never wrote ([#677](https://github.com/nathom/streamrip/issues/677), [#701](https://github.com/nathom/streamrip/issues/701)).
- Up to four attempts with backoff; retries resume the partial file
  ([#951](https://github.com/nathom/streamrip/issues/951), [#1022](https://github.com/nathom/streamrip/issues/1022), [PR #1009](https://github.com/nathom/streamrip/pull/1009)). Downloads run in a worker thread so they no
  longer block each other ([PR #982](https://github.com/nathom/streamrip/pull/982)).
- A failed conversion keeps the original, says why (ffmpeg's own error), leaves
  no temp file behind and still records the download
  ([#1010](https://github.com/nathom/streamrip/issues/1010)); AAC falls back to ffmpeg's own encoder ([PR #990](https://github.com/nathom/streamrip/pull/990)); lossy
  conversions honour `lossy_bitrate` ([#823](https://github.com/nathom/streamrip/issues/823), [PR #960](https://github.com/nathom/streamrip/pull/960)); OGG/Opus
  keep cover art ([PR #992](https://github.com/nathom/streamrip/pull/992)); new AIFF target ([PR #1006](https://github.com/nathom/streamrip/pull/1006)); ffmpeg can no longer
  break the terminal ([PR #996](https://github.com/nathom/streamrip/pull/996)).
- `[metadata] exclude` works ([#850](https://github.com/nathom/streamrip/issues/850)) and drops only the tags it names; MP3s are written as ID3v2.3 as intended.
  M4A files get their composer tag (it was written to the year's atom and
  lost), and MP3s no longer get the album description as their grouping
  (TIT1) or an empty lyrics frame.
- `restrict_characters` applies to every folder streamrip names: a single
  track saved with `add_singles_to_folder` lands in its album's folder
  instead of one beside it, and playlist folders follow it too.
- Long titles no longer fail with "File name too long" ([#856](https://github.com/nathom/streamrip/issues/856), [#859](https://github.com/nathom/streamrip/issues/859),
  [PR #860](https://github.com/nathom/streamrip/pull/860)); playlist names with `/` no longer create folders ([PR #1004](https://github.com/nathom/streamrip/pull/1004)).
- Artwork downloads work with current aiohttp headers ([#941](https://github.com/nathom/streamrip/issues/941)).
- `track_format` gains `{album}`, `{albumtitle}` and `{discnumber}` ([PR #826](https://github.com/nathom/streamrip/pull/826)).
- Error messages include the exception type ([#938](https://github.com/nathom/streamrip/issues/938)); Ctrl-C stops cleanly and
  removes temporary artwork ([PR #1025](https://github.com/nathom/streamrip/pull/1025)).
- CDNs sending more than 100 headers no longer break downloads ([PR #1033](https://github.com/nathom/streamrip/pull/1033)).
- Tracks with multiple credited artists (Tidal, Deezer) get a real
  multi-valued ARTIST tag (separate FLAC/Vorbis fields, a proper multi-value
  MP4 atom, "/"-joined for ID3v2.3) instead of one "A, B" string that players
  had to re-split themselves, and often got wrong.
- If a lossless copy of a track is already on disk, a lossy (AAC/MP3) copy
  of it is no longer downloaded; if the lossy copy already exists and a
  lossless one lands afterwards (also after a lossless conversion), the lossy
  one is removed. Avoids ending up with both a FLAC and an AAC copy of the
  same track after a quality setting change or a re-run. A file only counts
  as a copy when its title and album tags match, not just its name, and ALAC
  `.m4a` files are recognised as lossless.
- New `[metadata] prefer_explicit`: when an album or playlist lists both a
  clean and an explicit copy of the same track, only the explicit one is
  downloaded. Off by default. With it on, every track is resolved before the
  first one starts downloading, so downloads start a bit later; the number
  of API calls stays the same. Also fixed: Qobuz tracks were never marked
  explicit (`parental_warning` was ignored), which this option depends on.
- A connection that goes quiet for 30 seconds times out (all sources) instead
  of holding its album for aiohttp's default of five minutes.
- Artist downloads keep four albums in flight with a sliding window instead of
  waiting for the slowest album of every batch of four, and one failing album no
  longer aborts the rest. At most four tracks of an album are resolved at once,
  so downloads start after the first few tracks instead of after the metadata,
  lyrics and stream lookups of every track.
- An album whose tracks are all in the database is skipped without fetching a
  cover or creating a folder, and skipped tracks are reported once per album
  instead of once per track.
- New `streamrip database clear downloads|failed|all` forgets what was
  downloaded, for when you deleted the files and want them again.
- An unknown release date gives the year "Unknown" instead of "Unkn" (in tags
  and folder names), and no date tag instead of the text "Unknown".
- The repeats filter no longer crashes an artist download on an album whose
  title starts with a bracket, e.g. "(What's the Story) Morning Glory?";
  `[qobuz_filters] non_albums` now actually skips single-track releases.
- Label downloads use the same sliding window as artists, and one failing album
  no longer aborts the label.
- Playlists resolve through a sliding window too, instead of batches of 20 that
  waited for their slowest track. A playlist track that fails is recorded for
  `streamrip repair` like an album track. For last.fm playlists, one search
  error no longer aborts the playlist, and falls back to the fallback source.
- A playlist downloaded with `set_playlist_to_album` shows up as one album in
  Navidrome, Plex and the like: its tracks share the album artist "Various
  Artists" and are marked as a compilation, instead of splitting into one
  album per artist ([PR #738](https://github.com/nathom/streamrip/pull/738)). With `renumber_playlist_tracks`, the disc number
  and track total follow the playlist too.
- One URL that fails to resolve no longer stops the others; `streamrip file`
  keeps the order of its URLs; `search --first` with no results no longer
  crashes; `database browse failed` lines its columns up with the headers.

### Configuration

- Options that never did anything are gone: `[tidal] download_videos`,
  `[deezer] use_deezloader` and `deezloader_warnings`, the `[youtube]` section,
  `[cli] text_output` and `[soundcloud] quality` (SoundCloud has one quality).
- Options that did the same thing are merged: `[downloads] concurrency = false`
  is `max_connections = 1`; `[qobuz_filters]` is `[artist_filters]`, since it
  applies to every source, and its `non_studio_albums` (extras plus
  various-artists compilations) is part of `extras`.
- The config version is 2.3.3: an existing config is updated automatically on
  the next run, keeping your settings, including those of merged options.
- `[cli] max_search_results` is used (it was ignored): it's the default for
  `search --num-results`, in the interactive menu too.

### Development

- CI on Python 3.14, install smoke test on Linux, Windows and macOS,
  CodeQL on the right branch; all actions pinned to SHAs.
- Renovate keeps dependencies current; Dependabot handles security updates.
