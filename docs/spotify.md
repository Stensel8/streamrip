[← Back to the README](../README.md)

# Spotify

Spotify encrypts its audio, so streamrip doesn't download from Spotify itself.
It works like [spotDL](https://github.com/spotDL/spotify-downloader) does.
Spotify supplies the metadata: title, artists, album, track number, ISRC, cover,
and which tracks belong to an album, artist or playlist. streamrip then finds the
same recording on **YouTube Music** (by ISRC first, then by artist, title and
length), downloads it and tags it with Spotify's metadata. That means:

- The audio is whatever YouTube Music has: AAC at about 128 kbps
  (`audio_format = "m4a"`, kept as is) or MP3 at `audio_bitrate`. It isn't
  lossless, and it isn't Spotify's own quality either.
- If YouTube Music doesn't have a track, or only has a different version (live,
  remix, ...), streamrip reports it as not found instead of downloading the wrong
  thing.
- **For lossless, download the list from another source.**
  `streamrip playlist download spotify --playlist NAME --source qobuz` (or
  `--liked`) reads the list from Spotify and finds each track on Qobuz, Tidal or
  Deezer. `streamrip playlist transfer spotify tidal` moves it to Tidal for good.
  See [Playlists](playlists.md). streamrip reminds you of this the first time you
  use Spotify in a run.
- Like every source it needs [ffmpeg](https://ffmpeg.org). It also needs yt-dlp,
  which needs a JavaScript runtime to read YouTube. If a download fails and says
  so, install [Deno](https://deno.com) or [Node.js](https://nodejs.org).

## Setting up a Spotify app

Since February 2026, Spotify has no shared login for other programs. You create
a small app of your own in Spotify's developer dashboard, and **the account that
owns the app needs Spotify Premium**. Without Premium, Spotify refuses every
request. If you don't have it, someone who does can create the app and add your
Spotify account under *Settings > User Management*. It takes about a minute:

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   log in and choose **Create app**. The name and description can be anything.
2. Set the **Redirect URI** to exactly `http://127.0.0.1:9900/callback`. Spotify
   no longer accepts `localhost`.
3. Under *Which API/SDKs are you planning to use?*, tick only **Web API**.

   ![Only Web API is ticked.](images/spotify-web-api-ticked.avif)

4. Save, open the app's *Settings* and copy the **Client ID**. You don't need the
   client secret.

Now run streamrip with any Spotify link. The first time, it asks for the Client
ID and opens your browser so you can log in. streamrip picks up the response
itself on `127.0.0.1:9900`. If streamrip runs on another machine, you can paste
the address your browser ends up on instead. The login is saved in the
`[spotify]` section of the config (the Client ID as `client_id`) and renews
itself. The [playlist tools](playlists.md) use the same app and login, and you
can also set it up on the Accounts page of [`streamrip web`](web.md).

![What you see without Premium: "Your application is blocked from accessing the Web API since you do not have a Spotify Premium subscription."](images/spotify-premium-required.avif)

Without Premium, the dashboard shows this banner and every request fails with
HTTP 403.

```bash
streamrip url https://open.spotify.com/album/<album id>
streamrip url https://open.spotify.com/playlist/<id of a playlist of yours> spotify:track:<track id>
streamrip search spotify track 'daft punk get lucky'
```

## What works

Spotify limits what apps like this can do:

- **Tracks, albums, artists and search** work for anything on Spotify. Search
  returns at most 50 results.
- **Playlists** only work if you **own them or collaborate on them**. Spotify
  blocks the tracks of playlists you only follow, including its own editorial
  ones. Copy those into a playlist of your own first.
- Podcasts and local files in a playlist are skipped.
- At most five people can use an app: the ones you add under *User Management*.
  That's Spotify's limit for apps in development mode.

The options are in the `[spotify]` section of `streamrip config open`:
`audio_format` (`m4a` or `mp3`), `audio_bitrate` (only used when audio has to be
re-encoded), `match_videos` (accept an official video when YouTube Music has no
matching song; turn it off to be strict) and `redirect_uri`.

## Credits

The Spotify support borrows from three projects. Thank you:

- **[Music-Sync](https://opencommit.eu/Stensel8/Music-Sync)** (AGPL-3.0, same
  author), now part of streamrip as `streamrip playlist`. The Spotify login and
  API handling follow it: OAuth with PKCE and a loopback redirect
  (`streamrip/rip/spotify_login.py`, `streamrip/client/spotify.py`), token
  refresh, and Spotify's rules for development-mode apps since February 2026
  (Premium for the app's owner, ten search results per request, playlist tracks
  under `item`, only your own playlists). Matching a Spotify track to a YouTube
  Music result (`streamrip/client/audio_match.py`) is adapted from its
  `matching.py`, and the screenshots above come from its README.
- **[spotDL](https://github.com/spotDL/spotify-downloader)** (MIT), for the idea
  of taking only the metadata from Spotify and the audio from YouTube Music, and
  of looking a track up by ISRC first and then scoring candidates by name,
  artist, album and length (`streamrip/client/ytmusic.py`). The loopback
  redirect on `127.0.0.1` for the login comes from it too.
- **[MediaHarbor](https://github.com/MediaHarbor/mediaharbor)** (GPL-3.0), used
  as a reference for how an app talks to Spotify's Web API: client credentials
  and user tokens, following `next` links to the end of a list, and backing off
  when Spotify answers 429. No code is taken from it. MediaHarbor gets Spotify's
  own audio by decrypting the Widevine-protected streams with a device file.
  streamrip doesn't do that and never decrypts Spotify's audio.
