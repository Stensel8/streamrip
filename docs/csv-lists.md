[← Back to the README](../README.md)

# CSV lists

`streamrip csv list.csv` downloads the tracks in a CSV list by searching for them
on a source. It asks **which source to search and download from** (Qobuz, Tidal,
Deezer, Spotify or SoundCloud) and shows what each one offers. `--source tidal`
skips the question, which is handy in scripts, and `--fallback-source qobuz`
tries a second source for tracks the first one doesn't have. Name several,
`--fallback-source qobuz,deezer`, and they are tried in that order until one has
the track.

```bash
streamrip csv playlist.csv
streamrip csv playlist.csv --source tidal --fallback-source qobuz,deezer
```

The list can come from anywhere: `streamrip playlist export spotify --liked -o
liked.csv` ([Playlists](playlists.md)), Exportify, TuneMyMusic or your own
spreadsheet. For a Spotify or Tidal list, `streamrip playlist download` does both
steps in one go.

Each row is one track, with a title and an artist (separate several artists with
`;`). An album and a length (`duration_ms`) are optional, but make matches more
reliable. Columns are recognised by name: the ones `streamrip playlist export`
writes (`title`, `artists`, `album`, `duration_ms`) and those of other exporters
(`Track Name`, `Artist Name(s)`, `Album Name`, `Duration (ms)`, ...). A file
without a header row is read as `artist,title`. The file must be UTF-8. Commas,
semicolons (what Excel uses in many countries) and tabs all work as separators.

streamrip scores the first five search results on title, artist and length, like
the playlist tools do, and takes one that scores 0.8 or more. A live version, a
remix or a cover by another artist doesn't count. The track is then reported as
not found, by name.

The tracks go into a folder named after the file, like a last.fm playlist, and
the `[metadata]` options for playlists apply. Every track is a separate search,
paced by `requests_per_minute` in `[downloads]`, so a long list takes a while.
