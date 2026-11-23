[← Back to the README](../README.md)

# Playlists

`streamrip playlist` moves your liked songs and playlists between Spotify, Tidal
and CSV files, and can download them from a source of your choice. It used to be
[Music-Sync](https://github.com/Stensel8/Music-Sync). Everything here is also on
the *Playlists* page of [`streamrip web`](web.md).

| From \ to   | CSV      | Spotify    | Tidal      | Your music folder |
| ----------- | -------- | ---------- | ---------- | ----------------- |
| **Spotify** | `export` |            | `transfer` | `download`        |
| **Tidal**   | `export` | `transfer` |            | `download`        |
| **CSV**     |          | `import`   | `import`   | `streamrip csv`   |

Tracks are matched by ISRC, or by title, artist and length, and a live version
never counts as the studio recording ([details](#how-tracks-are-matched)).
Running the same transfer again doesn't add duplicates.

```bash
streamrip playlist login spotify                          # once per service
streamrip playlist login tidal

streamrip playlist transfer spotify tidal --sync-favorites   # liked songs, to your Tidal favourites
streamrip playlist transfer spotify tidal --liked         # liked songs, to a new playlist
streamrip playlist transfer spotify tidal --playlist "Road trip"
streamrip playlist download spotify --playlist "Road trip" --source qobuz   # lossless, from Qobuz
streamrip playlist export spotify --liked -o liked.csv
streamrip playlist export tidal --all -o backup/          # liked songs and every playlist
streamrip playlist export tidal --all --format m3u8 --library ~/Music -o ~/Music/Playlists
streamrip playlist import tidal liked.csv --playlist "From CSV"
streamrip playlist import tidal albums.csv --albums       # artist,album per row, to your favourite albums

streamrip playlist list spotify                           # your playlists, with their ids
streamrip playlist status                                 # what is set up and logged in
streamrip playlist doctor                                 # tries the real APIs
```

| Option              | What it does                                                          |
| ------------------- | --------------------------------------------------------------------- |
| `--dry-run`         | Look everything up, but change nothing                                |
| `--unmatched FILE`  | Save the tracks that weren't found to a CSV                           |
| `--min-score 0.8`   | How sure a match must be (0 to 1)                                     |
| `--to-playlist NAME`| The playlist to fill (transfer)                                       |
| `--to-favorites`    | Add to your favourites (liked songs) instead of a playlist (import)   |
| `--albums`          | The CSV lists albums; add them to your favourites (import)            |
| `-s`, `-fs`         | The source to download from, and a fallback for what it lacks (download) |
| `-q`                | No progress output                                                    |

`streamrip playlist download` covers what a Spotify link can't. Spotify encrypts
its audio, so a Spotify link gets you YouTube Music's AAC (see
[Spotify](spotify.md)). This command reads the list from Spotify and finds each
track on Qobuz, Tidal or Deezer instead, the same way
[`streamrip csv`](csv-lists.md) does. Each list gets its own folder.

## M3U8 playlists for your own library

`--format m3u8` writes playlist files for Navidrome, Jellyfin, Music Assistant,
or any player that reads a music folder, like the one streamrip downloads to.

With `--library DIR`, streamrip looks for each track among the audio files in
`DIR`. It checks the ISRC first (ripping tools rename files, but the ISRC stays)
and otherwise title, artist and length. The playlist then points to that file,
with a path relative to the playlist (`--absolute-paths` for absolute ones). If a
recording is in your library more than once, the oldest release is used (`-v`
tells you which). Tracks you don't have are left out. `--unmatched FILE` writes
them to a CSV, which you can then download with `streamrip csv`.

Without `--library`, the playlist only describes the tracks.

## Setting up Spotify and Tidal

### Spotify

Downloading and the playlist tools share one Spotify app and one login. Set up
the app as described in [Spotify](spotify.md#setting-up-a-spotify-app); the
account that owns it needs Premium. `streamrip playlist login spotify` is the
same login a Spotify link asks for. The playlist tools need a bit more access
(reading and changing your liked songs and playlists), so if you logged in
before streamrip had them, you'll be asked once more.

### Tidal

For downloading, streamrip uses the same login as Tidal's own apps. Your
playlists and favourites are only available through Tidal's official API, and
that needs a developer app of your own with a separate login.

1. Click *Create New App* in the [TIDAL developer dashboard](https://developer.tidal.com/dashboard).
2. Set the redirect URI to `http://127.0.0.1:9900/tidal/callback`.
3. Add the scopes `collection.read`, `collection.write`, `playlists.read`,
   `playlists.write` and `user.read`.
4. Copy the Client ID and the Client Secret. streamrip uses the secret to search
   Tidal's catalogue.

![The Scopes panel of a TIDAL app.](images/tidal-scopes.avif)

`streamrip playlist login tidal` asks for both the first time. You can also put
them in `config.toml` (`streamrip config open`):

```toml
[spotify]
client_id = "..."

[tidal]
api_client_id = "..."
api_client_secret = "..."
```

streamrip searches the Tidal catalogue of the country of your Tidal download
login, or of your system if you don't have one. All logins are stored in
`config.toml`, like the download logins, so keep that file private. streamrip
makes it readable only by you.

### Coming from Music-Sync

```bash
streamrip playlist migrate
```

copies the apps and logins from Music-Sync's settings (`~/.config/music-sync/`,
or `%APPDATA%\music-sync` on Windows) for anything streamrip doesn't have yet.
After that, add streamrip's redirect URIs to both apps. Music-Sync used port
8888, streamrip uses 9900, and `streamrip playlist status` shows the URIs. The
`music-sync` commands are now `streamrip playlist` commands (`playlists` became
`list`), and `music-sync web` is now [`streamrip web`](web.md).

## How tracks are matched

Tracks that are already in the target playlist are recognised without a lookup,
so a second run is quick. Other tracks are looked up by their id on the target
service (when the CSV was exported from it), then by ISRC, then by searching for
title and artist. As a last resort streamrip searches the album's tracklist, like
spotify_to_tidal does.

Each search result gets a score from 0 to 1. The title counts for half, the
artist for 0.4 and the length for 0.1. From 0.8 (`--min-score`) it's a match.

streamrip ignores remaster notes, "feat." credits, spelling and punctuation. It
does keep versions apart: live, remix, acoustic, instrumental, sped up,
"(Taylor's Version)" and so on, and numbers too ("Part 1" isn't "Part 2"). When a
track isn't found, streamrip tells you why:

| Reason                                       | What it means                                                       |
| -------------------------------------------- | ------------------------------------------------------------------- |
| not on Tidal                                 | Nothing by this artist turned up. It's probably not in your country's catalogue |
| only other songs on Tidal                    | The artist is there, but this song isn't                            |
| only another version                         | Only a live version, a remix or similar                             |
| score too low for a match (0.75, needs 0.80) | Close, but not close enough. Often the length differs               |

The same matching picks the YouTube Music result for a Spotify track, and the
search result for each row of a CSV list.

## CSV format

| Column                     | Contents                                          |
| -------------------------- | ------------------------------------------------- |
| `title`                    | Track title (the only required column)            |
| `artists`                  | Artists, separated by `;`                         |
| `album`                    | Album title                                       |
| `duration_ms`              | Length in milliseconds                            |
| `isrc`                     | ISRC, the most reliable way to find a track again |
| `spotify_uri` / `tidal_id` | The track's id on that service                    |

Files must be UTF-8. Column names from other exporters (`Track Name`,
`Artist Name(s)`, ...) work too, and cells can be separated by commas,
semicolons or tabs. A file without a header row is read as `artist,title`. With
`--albums`, each row is an album (`artist,album`), the way csv2tidal read them.

## Good to know

- Spotify only lets apps read playlists you own or collaborate on. Playlists you
  only follow are skipped with a note, and so are empty ones.
- Transfers to Tidal are fast (20 ISRCs per request). Transfers to Spotify take
  one request per track.
- Tidal's API is still young. If something breaks, add the output of
  `streamrip playlist doctor tidal` to your issue.

## Credits

The playlist tools come from Music-Sync, which started as
[csv2tidal](https://github.com/Nugman/csv2tidal), a small script by
[Nugman](https://github.com/Nugman) that added a list of albums to your Tidal
favourites. He got the idea from
[a gist by RZetko](https://gist.github.com/RZetko/71801a20188e842ef03bed3b6d7a297f)
and the login code from [spotify_to_tidal](https://github.com/spotify2tidal/spotify_to_tidal)
by [timrae](https://github.com/timrae) and
[its contributors](https://github.com/spotify2tidal/spotify_to_tidal/graphs/contributors).
We still borrow ideas from it. All three were built on
[python-tidal](https://github.com/EbbLabs/python-tidal), started by
[tamland](https://github.com/tamland) and kept alive by
[morguldir](https://github.com/morguldir) and
[tehkillerbee](https://github.com/tehkillerbee). [Stensel8](https://github.com/Stensel8) combined csv2tidal
with his own prototype into Music-Sync, and later merged Music-Sync into
streamrip. The colours of the web interface come from
[daisyUI](https://daisyui.com). For the licence, see
[License](../README.md#license).
