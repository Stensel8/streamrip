[← Back to the README](../README.md)

# Spotify

Spotify supplies the metadata: title, artists, album, track number, ISRC, cover,
and which tracks belong to an album, artist or playlist. The audio comes from one
of two backends, in the `[spotify]` section of the config, and streamrip picks a
working one for every track:

- `backend = "spotify"` (the default) plays each track in real time with
  Spotify's own official client,
  [Soloist](https://github.com/spotify/soloist), and records the local audio.
  That is Spotify's lossless FLAC (Premium), recorded at 44.1 kHz and saved as a
  24-bit FLAC. Because the track plays in full and is recorded as it goes, it
  takes the track's own length, a minute and a half to four, so it rips slower
  than the other sources. It runs on Linux only.
- `backend = "youtube"` finds the same recording on **YouTube Music** (by ISRC
  first, then by artist, title and length) and downloads it, the way
  [spotDL](https://github.com/spotDL/spotify-downloader) does. It is AAC at
  about 128 kbps (`audio_format = "m4a"`, kept as is) or MP3 at `audio_bitrate`,
  and it is not lossless.

A track the `spotify` backend cannot play, because it is not available to this
account, has no matching recording, or is a podcast episode, goes to YouTube
Music either way. So choosing it costs nothing, and a list is never half-empty.

Like every source it needs [ffmpeg](https://ffmpeg.org). The `youtube` backend
also needs yt-dlp, which needs a JavaScript runtime to read YouTube. If a
download fails and says so, install [Deno](https://deno.com) or
[Node.js](https://nodejs.org).

## Logging in

One login, in `state.toml` next to the config, does the downloads and the
[playlist tools](playlists.md) alike, and it renews itself. It needs **an app of
your own** from Spotify's developer dashboard, and **the account that owns the
app needs Spotify Premium**: without Premium, Spotify refuses every request. If
you don't have it, someone who does can create the app and add your Spotify
account under *Settings > User Management*. It takes about a minute:

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   log in and choose **Create app**. The name and description can be anything.
2. Set the **Redirect URI** to exactly `http://127.0.0.1:9900/callback`. Spotify
   no longer accepts `localhost`.
3. Under *Which API/SDKs are you planning to use?*, tick only **Web API**.

   ![Only Web API is ticked.](images/spotify-web-api-ticked.avif)

4. Save, open the app's *Settings* and copy the **Client ID**. You don't need the
   client secret.

Then `streamrip login spotify` asks for the Client ID and opens your browser so
you can log in. streamrip picks up the response itself on `127.0.0.1:9900`. If
streamrip runs on another machine, you can paste the address your browser ends
up on instead. The [web interface](web.md) does the same on its Accounts page.

![What you see without Premium: "Your application is blocked from accessing the Web API since you do not have a Spotify Premium subscription."](images/spotify-premium-required.avif)

Without Premium, the dashboard shows this banner and every request fails with
HTTP 403.

## Spotify's own audio (the `spotify` backend)

This one is on by default. It needs three things, each once or automatic:

**A Soloist API key.** One per person, free for a Premium account, made at
[developer.spotify.com/dashboard/soloist](https://developer.spotify.com/dashboard/soloist).
It is a secret, like a password. streamrip never prints it, logs it, or sends it
anywhere but Spotify. When it asks, paste it in, and it is saved as
`soloist_api_key`.

**A pairing, once.** In the Spotify app on a device on this network, press play,
tap *Connect to a device*, and pick the device named `device_name` (default
`streamrip`) to play a track. The session is stored on this machine and replayed
by every later run. The Accounts page of the [web interface](web.md) does this
too: paste the key, press *Pair*, and pick the device in the app.

**Linux, with PipeWire or PulseAudio.** The recording is a sink monitor, made
with `pactl` and `parec` (PulseAudio's tools, which PipeWire has too), so the
`spotify` backend runs on Linux for now. On macOS or Windows, or
if any of the above is missing, streamrip does not fail: it falls back to
YouTube Music for that track, so you still get the file.

streamrip sets two prefs in Soloist's data folder before each play: the lossless
quality (Soloist plays Ogg Vorbis at 160 kbps otherwise) and no volume
normalization (which turns the level down by some 6 dB). If you use that Soloist
as a player too, they apply there as well. If Spotify has no lossless file of a
track, streamrip says what it played instead.

streamrip keeps its own copy of the Soloist binary. The official builds expire
after 90 days, and when one does, streamrip downloads a fresh one and carries on.
There is nothing else to install.

```bash
streamrip url https://open.spotify.com/album/<album id>
streamrip url https://open.spotify.com/playlist/<id of a playlist of yours> spotify:track:<track id>
streamrip search spotify track 'daft punk get lucky'
```

To go lossy on purpose, for a device that wants MP3, set `backend = "youtube"`.

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
`backend` (`spotify` or `youtube`), `soloist_api_key` and `device_name` for the
`spotify` backend, and `audio_format` (`m4a` or `mp3`), `audio_bitrate` (only
used when audio has to be re-encoded) and `match_videos` (accept an official
video when YouTube Music has no matching song; turn it off to be strict) for the
`youtube` backend, and `redirect_uri` (for your own app).

## Credits

The Spotify support borrows from a few projects. Thank you:

- **[Spotify Soloist](https://github.com/spotify/soloist)** (MIT), Spotify's own
  official client. A Connect device that plays a track and renders it to local
  audio at the account's quality. streamrip drives it and records the result
  (`streamrip/client/soloist.py`). It plays Spotify's own audio.
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
  of taking the metadata from Spotify and the audio from YouTube Music, and of
  looking a track up by ISRC first and then scoring candidates by name, artist,
  album and length (`streamrip/client/ytmusic.py`). The loopback redirect on
  `127.0.0.1` for the login comes from it too.
- **[MediaHarbor](https://github.com/MediaHarbor/mediaharbor)** (GPL-3.0), used
  as a reference for how an app talks to Spotify's Web API: client credentials
  and user tokens, following `next` links to the end of a list, and backing off
  when Spotify answers 429. No code is taken from it.
