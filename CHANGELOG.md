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
- `download_booklets` works: an album's PDF booklets are saved in its folder.
  The option was there (and on) but nothing downloaded them since v1.

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
- `[metadata] exclude` works ([#850](https://github.com/nathom/streamrip/issues/850)) and drops only the tags it names; MP3s are written as ID3v2.3 as intended.
  M4A files get their composer tag (it was written to the year's atom and
  lost), and MP3s no longer get the album description as their grouping
  (TIT1) or an empty lyrics frame.
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
- The config version is 2.3.2: an existing config is updated automatically on
  the next run, keeping your settings, including those of merged options.
- `[cli] max_search_results` is used (it was ignored): it's the default for
  `search --num-results`, in the interactive menu too.

### Development

- CI on Python 3.14, install smoke test on Linux, Windows and macOS,
  CodeQL on the right branch; all actions pinned to SHAs.
- Renovate keeps dependencies current; Dependabot handles security updates.
