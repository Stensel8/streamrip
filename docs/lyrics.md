[← Back to the README](../README.md)

# Lyrics

Tidal provides lyrics for most tracks, and streamrip embeds them. Qobuz, Deezer,
SoundCloud and Spotify don't provide any, so for those tracks streamrip looks the
lyrics up on [LRCLIB](https://lrclib.net), a free and open lyrics database filled
by its users. It needs no account or key. The lookup happens once the track has
downloaded.

For every track without lyrics, streamrip **sends the artist, title and length
to LRCLIB**. Set `lyrics = false` in `[downloads]` or `exclude = ["lyrics"]` in
`[metadata]` if you don't want that.

The length comes from the downloaded file, because synced lyrics only fit one
edit of a song. A result has to be within a few seconds of it, and lyrics for
another version (live, remix, ...) or another artist are skipped. If LRCLIB
doesn't have the track, it stays without lyrics and streamrip says so ("No
lyrics found for ..."). A failed lookup never fails the track, and after three
failures in a row streamrip stops asking LRCLIB for the rest of the run.

Synced (LRC) lyrics go into M4A and FLAC files, plain lyrics into MP3s, also
when you convert to MP3. Lyrics that came from the source are left as they are.

Lyrics belong to their owners, and LRCLIB's are added by its users. This is
meant for your own library.
