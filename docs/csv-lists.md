[← Back to the README](../README.md)

# CSV lists

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
