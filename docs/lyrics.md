[← Back to the README](../README.md)

# Lyrics

Tidal and Deezer send lyrics for most tracks, and streamrip embeds them
(time-synced ones as LRC text, plain ones in an MP3). Qobuz, SoundCloud and
Spotify send none, so for a track whose source sent no lyrics streamrip looks them
up on
[LRCLIB](https://lrclib.net), a free, open database of lyrics that its users add.
It needs no account or key.

- It **tells LRCLIB the artist, title and length** of every track that has no
  lyrics. `lyrics = false` in `[downloads]` keeps that from happening.
- The length is that of the file just downloaded, as time-synced lyrics only fit
  one edit of a song: a record has to be within a few seconds of it, and one of
  another version (live, remix, ...) or of another artist is not taken. A track
  LRCLIB does not have stays without lyrics, and streamrip says so ("No lyrics
  found for ..."). A lookup that fails never fails a track; after three failures
  in a row LRCLIB is left alone for the run.
- Synced (LRC) lyrics go in M4A and FLAC, plain ones in an MP3 (also when you
  convert to MP3). Lyrics a source did send are left alone, and `lyrics = false`
  in `[downloads]` or `exclude = ["lyrics"]` in `[metadata]` keeps any lookup
  from happening.
- Lyrics stay the property of their owners, and LRCLIB's come from its users:
  this is for your own library.
