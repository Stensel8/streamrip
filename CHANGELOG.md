# Changelog

All notable changes in this fork of [nathom/streamrip](https://github.com/nathom/streamrip). Numbers refer to
upstream issues and pull requests.

## 2.3.0

### Installation and dependencies

- Targets current Python (3.14+) only; every dependency is on a current
  release with wheels for 3.14. Pillow was capped below 11, which has no 3.14
  wheels and failed to build ([#1032](https://github.com/nathom/streamrip/issues/1032), [#953](https://github.com/nathom/streamrip/issues/953), [#904](https://github.com/nathom/streamrip/issues/904)).
- `pyproject.toml` uses PEP 621 metadata (Poetry 2).
- `appdirs` and `aiodns` are no longer dependencies. `aiodns` forced Windows onto
  an event loop that cannot run ffmpeg, so conversions failed there ([#729](https://github.com/nathom/streamrip/issues/729)).
- HTTP(S)_PROXY / ALL_PROXY environment variables are honoured ([#961](https://github.com/nathom/streamrip/issues/961)).
- Install from GitHub; the update notice points at this fork instead of telling
  you to `pip install streamrip`, which would install upstream.
- The CLI command is `streamrip`, not `rip`: short names like `rip` collide
  with other tools and local shell aliases/functions too easily.

### Qobuz

- Albums download again after Qobuz stopped returning track lists from
  `album/get` and `playlist/get` ([#1012](https://github.com/nathom/streamrip/issues/1012), [PR #1013](https://github.com/nathom/streamrip/pull/1013)).
- Token login: the prompt asks for a user id and `user_auth_token` because the
  password login is behind a captcha now ([#954](https://github.com/nathom/streamrip/issues/954), [#956](https://github.com/nathom/streamrip/issues/956), [#899](https://github.com/nathom/streamrip/issues/899),
  [#854](https://github.com/nathom/streamrip/issues/854)); optional browser capture with `pip install 'streamrip[qobuz-login]'`
  ([PR #955](https://github.com/nathom/streamrip/pull/955)).
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

### Tidal

- Lossless tracks download as FLAC again instead of AAC 320 ([#966](https://github.com/nathom/streamrip/issues/966),
  [#897](https://github.com/nathom/streamrip/issues/897), [#968](https://github.com/nathom/streamrip/issues/968),
  [PR #1017](https://github.com/nathom/streamrip/pull/1017)); optional hi-res client via `hires_client = true`, and hi-res tracks
  served as MPEG-DASH are downloaded and remuxed instead of silently downgrading to AAC
  ([#974](https://github.com/nathom/streamrip/issues/974), [PR #998](https://github.com/nathom/streamrip/pull/998)). Changing
  the client asks for a new login once.
- Tracks without lyrics are no longer dropped, and lyrics errors never abort a
  track ([#983](https://github.com/nathom/streamrip/issues/983), [#959](https://github.com/nathom/streamrip/issues/959), [#866](https://github.com/nathom/streamrip/issues/866), [PR #1036](https://github.com/nathom/streamrip/pull/1036), [PR #1024](https://github.com/nathom/streamrip/pull/1024)).
- Share links ending in `/u` work ([PR #911](https://github.com/nathom/streamrip/pull/911)).
- Expired or revoked logins offer a fresh login instead of a traceback
  ([#896](https://github.com/nathom/streamrip/issues/896), [#906](https://github.com/nathom/streamrip/issues/906), [#901](https://github.com/nathom/streamrip/issues/901), [PR #1029](https://github.com/nathom/streamrip/pull/1029)); refreshed tokens are saved.
- `null` copyright no longer crashes ([PR #979](https://github.com/nathom/streamrip/pull/979)); unknown quality values
  (`HI_RES_LOSSLESS`) no longer raise `KeyError`.
- Search returned nothing when there was exactly one hit, which broke last.fm
  playlists on Tidal.

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

### SoundCloud

- The client id is found again ([#1038](https://github.com/nathom/streamrip/issues/1038)); tracks without an MP3 HLS stream use
  the progressive stream or are skipped instead of crashing.
- HLS segments are reassembled in their original order regardless of which one
  finishes downloading first; concurrent, out-of-order completion produced
  skips and reordered audio ([#848](https://github.com/nathom/streamrip/issues/848), [#633](https://github.com/nathom/streamrip/issues/633)).

### Downloads, conversion and tagging

- Failed downloads are no longer marked as downloaded, resolve failures are
  recorded, and `streamrip repair` retries everything in the failed database
  ([PR #1023](https://github.com/nathom/streamrip/pull/1023)). A track skipped
  after a 403 (e.g. a geoblocked Deezer track in an otherwise public playlist)
  no longer leaves the run in a state where cleanup trips over the file it
  never wrote ([#677](https://github.com/nathom/streamrip/issues/677), [#701](https://github.com/nathom/streamrip/issues/701)).
- Up to four attempts with backoff; retries resume the partial file
  ([#951](https://github.com/nathom/streamrip/issues/951), [#1022](https://github.com/nathom/streamrip/issues/1022), [PR #1009](https://github.com/nathom/streamrip/pull/1009)). Downloads run in a worker thread so they no
  longer block each other ([PR #982](https://github.com/nathom/streamrip/pull/982)).
- A failed conversion keeps the original and still records the download
  ([#1010](https://github.com/nathom/streamrip/issues/1010)); AAC falls back to ffmpeg's own encoder ([PR #990](https://github.com/nathom/streamrip/pull/990)); lossy
  conversions honour `lossy_bitrate` ([#823](https://github.com/nathom/streamrip/issues/823), [PR #960](https://github.com/nathom/streamrip/pull/960)); OGG/Opus
  keep cover art ([PR #992](https://github.com/nathom/streamrip/pull/992)); new AIFF target ([PR #1006](https://github.com/nathom/streamrip/pull/1006)); ffmpeg can no longer
  break the terminal ([PR #996](https://github.com/nathom/streamrip/pull/996)).
- `[metadata] exclude` works ([#850](https://github.com/nathom/streamrip/issues/850)); MP3s are written as ID3v2.3 as intended.
- Long titles no longer fail with "File name too long" ([#856](https://github.com/nathom/streamrip/issues/856), [#859](https://github.com/nathom/streamrip/issues/859),
  [PR #860](https://github.com/nathom/streamrip/pull/860)); playlist names with `/` no longer create folders ([PR #1004](https://github.com/nathom/streamrip/pull/1004)).
- Artwork downloads work with current aiohttp headers ([#941](https://github.com/nathom/streamrip/issues/941)).
- `track_format` gains `{album}`, `{albumtitle}` and `{discnumber}` ([PR #826](https://github.com/nathom/streamrip/pull/826)).
- Error messages include the exception type ([#938](https://github.com/nathom/streamrip/issues/938)); Ctrl-C stops cleanly and
  removes temporary artwork ([PR #1025](https://github.com/nathom/streamrip/pull/1025)).
- CDNs sending more than 100 headers no longer break downloads ([PR #1033](https://github.com/nathom/streamrip/pull/1033)).

### Development

- CI on Python 3.14, install smoke test on Linux, Windows and macOS,
  CodeQL on the right branch; all actions pinned to SHAs.
- Renovate keeps dependencies current; Dependabot handles security updates.
