[← Back to the README](../README.md)

# Playlists

`streamrip playlist` moves your liked songs and playlists between Spotify, Tidal
and CSV files, and downloads them from the source you pick. It was
[Music-Sync](https://github.com/Stensel8/Music-Sync), which is part of streamrip
now; the same is on the *Playlists* page of [`streamrip web`](web.md).

| From \ to   | CSV      | Spotify    | Tidal      | Your music folder |
| ----------- | -------- | ---------- | ---------- | ----------------- |
| **Spotify** | `export` |            | `transfer` | `download`        |
| **Tidal**   | `export` | `transfer` |            | `download`        |
| **CSV**     |          | `import`   | `import`   | `streamrip csv`   |

Each track is found by its ISRC, or else by title, artist and length, and a live
version is never taken for the studio one. Running a transfer twice adds
nothing twice.

```bash
streamrip playlist login spotify                          # once per service
streamrip playlist login tidal

streamrip playlist transfer spotify tidal --sync-favorites   # liked songs, to your Tidal favourites
streamrip playlist transfer spotify tidal --liked         # liked songs, to a new playlist
streamrip playlist transfer spotify tidal --playlist "Road trip"
streamrip playlist download spotify --playlist "Road trip" --source qobuz   # lossless, from Qobuz
streamrip playlist export spotify --liked -o liked.csv
streamrip playlist export tidal --all -o backup/          # liked songs and every playlist
streamrip playlist import tidal liked.csv --playlist "From CSV"
streamrip playlist import tidal albums.csv --albums       # artist,album per row, to your favourite albums

streamrip playlist list spotify                           # your playlists, with their ids
streamrip playlist status                                 # what is set up and logged in
streamrip playlist doctor                                 # tries the real APIs
```

| Option              | What it does                                                          |
| ------------------- | --------------------------------------------------------------------- |
| `--dry-run`         | Look everything up, change nothing                                    |
| `--unmatched FILE`  | Save the tracks that were not found to a CSV                          |
| `--min-score 0.8`   | How sure a match must be (0 to 1)                                     |
| `--to-playlist NAME`| The playlist to fill (transfer)                                       |
| `--to-favorites`    | Add to your favourites (liked songs) instead of a playlist (import)   |
| `--albums`          | The CSV lists albums, not tracks; add them to your favourites (import)|
| `-s`, `-fs`         | The source to download from, and the one for what it lacks (download) |
| `-q`                | No progress output                                                    |

`streamrip playlist download` is what a Spotify link cannot do: Spotify's own
audio is encrypted, so a Spotify link downloads YouTube Music's AAC (see
[Spotify](spotify.md)). This reads the list from Spotify and finds every track
on Qobuz, Tidal or Deezer instead, the way [`streamrip csv`](csv-lists.md)
does. Each list goes in a folder of its own.

## Setting up Spotify and Tidal

### Spotify

One app and one login, for downloading and for the playlist tools alike: the
app is the one in [Spotify](spotify.md), and `streamrip playlist login spotify`
is the same login as a Spotify link asks for. It asks for a little more than
downloading needs (reading and changing your liked songs and playlists), so a
login made before streamrip had these tools is asked for again once.

> [!IMPORTANT]
> Since February 2026 Spotify's API only works for apps whose owner has
> **Premium**. Without it every request fails with HTTP 403. Someone with
> Premium can make the app and add you under *Settings > User Management*.

1. Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Redirect URI: `http://127.0.0.1:9900/callback` (`127.0.0.1`, not `localhost`).
3. Under *Which API/SDKs are you planning to use?* tick **Web API** only.
4. Copy the Client ID. The client secret is not needed.

![Only Web API is ticked.](images/spotify-web-api-ticked.avif)

### Tidal

Downloading from Tidal uses the login Tidal's own apps use; your playlists and
favourites are only reachable through Tidal's official API, which needs a
developer app of your own and a login of its own.

1. Click *Create New App* in the [TIDAL developer dashboard](https://developer.tidal.com/dashboard).
2. Redirect URI: `http://127.0.0.1:9900/tidal/callback`.
3. Scopes: `collection.read`, `collection.write`, `playlists.read`,
   `playlists.write` and `user.read`.
4. Copy the Client ID and the Client Secret. The secret lets streamrip search
   Tidal's catalogue.

![The Scopes panel of a TIDAL app.](images/tidal-scopes.avif)

`streamrip playlist login tidal` asks for both the first time, or put them in
`config.toml` (`streamrip config open`):

```toml
[spotify]
client_id = "..."

[tidal]
api_client_id = "..."
api_client_secret = "..."
```

The Tidal catalogue searched is that of the country of your Tidal download
login, or else of your system. Every login is kept in `config.toml`, like the
download logins: keep that file private (streamrip makes it readable to you
alone).

### Coming from Music-Sync

```bash
streamrip playlist migrate
```

takes the apps and logins from Music-Sync's settings (`~/.config/music-sync/`, on
Windows `%APPDATA%\music-sync`) where streamrip has none of its own. Then add
streamrip's redirect URIs to both apps: Music-Sync used port 8888, streamrip
9900. `streamrip playlist status` shows them. The `music-sync` commands are
`streamrip playlist` commands now (`playlists` is `list`), and `music-sync web`
is [`streamrip web`](web.md).

## How tracks are matched

A track that is already in the target playlist is recognised without a lookup,
so a second run is quick. Any other track is found by its id on the target
service (a CSV exported from it), else by its ISRC, else by searching for title
and artist, and as a last resort in the tracklist of its album, as
spotify_to_tidal does. Each search result gets a score from 0 to 1: half for the
title, 0.4 for the artist and 0.1 for the length. From 0.8 on (`--min-score`) it
is a match.

Remaster notes, "feat." parts, spelling and punctuation do not count. Another
version does: live, remix, acoustic, instrumental, sped up, "(Taylor's Version)"
and the like, and other numbers ("Part 1" is not "Part 2"). A track that is not
found says why:

| Reason                                       | What it means                                                       |
| -------------------------------------------- | ------------------------------------------------------------------- |
| not on Tidal                                 | Nothing by this artist came up: probably not in your country's catalogue |
| only other songs on Tidal                    | The artist is there, this song is not                               |
| only another version                         | Only a live version, remix and the like                             |
| score too low for a match (0.75, needs 0.80) | Close, but not sure enough; often the length differs                |

The same matching decides which YouTube Music result is a Spotify track, and
which search result is a row of a CSV list.

## CSV format

| Column                     | Contents                                          |
| -------------------------- | ------------------------------------------------- |
| `title`                    | Track title (the only column that is required)    |
| `artists`                  | Artists, separated by `;`                         |
| `album`                    | Album title                                       |
| `duration_ms`              | Length in milliseconds                            |
| `isrc`                     | ISRC, the most reliable way to find a track again |
| `spotify_uri` / `tidal_id` | The track's id on that service                    |

Files must be UTF-8. The column names of other exporters (`Track Name`,
`Artist Name(s)`, ...) work too, and so do semicolons and tabs between the
cells; a file without a header row is read as `artist,title`. With `--albums`
each row is an album: `artist,album`, as csv2tidal took them.

## Good to know

- Spotify only lets apps read playlists you own or collaborate on, not the
  ones you follow. They are skipped, and so are empty playlists, with a note.
- A transfer to Tidal is fast (20 ISRCs per request); one to Spotify takes a
  request per track.
- Tidal's API is young. If something fails, add the output of
  `streamrip playlist doctor tidal` to your issue.

## Credits

The playlist tools were Music-Sync, which started out as
[csv2tidal](https://github.com/Nugman/csv2tidal), a small script by Roland Behme
([Nugman](https://github.com/Nugman)) that put a list of albums in your Tidal
favourites. He took the idea from
[a gist by RZetko](https://gist.github.com/RZetko/71801a20188e842ef03bed3b6d7a297f),
and the login code from [spotify_to_tidal](https://github.com/spotify2tidal/spotify_to_tidal)
by [Tim Rae](https://github.com/timrae) and
[its contributors](https://github.com/spotify2tidal/spotify_to_tidal/graphs/contributors),
which we still borrow ideas from. All three ran on
[python-tidal](https://github.com/EbbLabs/python-tidal), started by
[Thomas Amland](https://github.com/tamland) and kept alive by
[morguldir](https://github.com/morguldir) and
[tehkillerbee](https://github.com/tehkillerbee). Sten Tijhuis merged csv2tidal
with his own prototype into Music-Sync, and Music-Sync into streamrip. The
colours of the web interface come from [daisyUI](https://daisyui.com). How this
is licensed: [LICENSE](../LICENSE).
