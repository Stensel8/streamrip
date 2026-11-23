[← Back to the README](../README.md)

# Spotify

Spotify gives streamrip the track lists, the tags, the cover art and the audio.
There are two ways to get the audio (the `[spotify]` `backend`), and streamrip
picks a working one for every track:

- **`backend = "spotify"`** (the default) plays each track in real time with
  Spotify's own official headless client,
  [Soloist](https://github.com/spotify/soloist), and streamrip saves the local
  audio as the account hears it — the account's own quality, lossless on
  Premium — into a file. It is not re-encoded down: a Premium account gets its
  Premium audio.
- **`backend = "youtube"`** (always available; lossy) finds the same recording
  on **YouTube Music** — by its ISRC first, then by artist, title and length —
  and downloads that from there, the way spotDL does. It is AAC of about
  128 kbps (`audio_format = "m4a"`, kept as it is) or MP3 at `audio_bitrate`,
  and it is **not lossless**.

A track the `spotify` backend cannot play — not available to this account, no
matching recording, a podcast episode — is routed to YouTube Music either way,
so choosing it costs nothing. Nothing is lost, and a list is never half-empty.

|             | `spotify` (the default)        | `youtube`                    |
| ----------- | ------------------------------ | ---------------------------- |
| Audio       | The account's own quality (lossless on Premium) | AAC ~128 kbps or MP3 |
| Re-encoded  | No — kept as the account plays it | To `audio_format` if needed |
| Platform    | **Linux only** (records a PipeWire/PulseAudio sink) | Any |
| Extra login | A Soloist API key + one pairing | —                           |
| Works on    | Premium and Free (the tier decides the quality) | Any |

It still needs [ffmpeg](https://ffmpeg.org) like every source; the `youtube`
backend also uses yt-dlp, which wants a JavaScript runtime to read YouTube —
install [Deno](https://deno.com) or [Node.js](https://nodejs.org) if a download
fails saying so.

## The metadata login (both backends need it)

**Spotify's own rules for logging in** (since February 2026): there is no
shared login for outside programs, so you make a small app of your own in
Spotify's developer dashboard, and **the account that owns the app must have
Spotify Premium**. Without Premium Spotify refuses every request. If you have
no Premium, someone who does can make the app and add your Spotify account
under *Settings > User Management*. Making the app takes a minute:

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   log in and choose **Create app**. Name and description can be anything.
2. **Redirect URI**: `http://127.0.0.1:9900/callback`, character for character.
   It has to be `127.0.0.1`, not `localhost`; Spotify no longer accepts that.
3. Under *Which API/SDKs are you planning to use?* tick **Web API** only.

   ![Only Web API is ticked.](images/spotify-web-api-ticked.avif)

4. Save, open the app's *Settings* and copy its **Client ID**. You do not need
   the client secret.

Then run streamrip with any Spotify link. The first time it asks for the Client
ID and opens your browser to log in; it catches the answer itself on
`127.0.0.1:9900` (if streamrip runs on another machine, it offers to let you paste
the address you end up on instead). The login is saved in the `[spotify]`
section of the config and renews itself. If you had to give the Client ID by
hand, it goes in `client_id` there. The same app and login serve the
[playlist tools](playlists.md), and the Accounts page of
[`streamrip web`](web.md) sets them up too.

![What you see without Premium: "Your application is blocked from accessing the Web API since you do not have a Spotify Premium subscription."](images/spotify-premium-required.avif)

(Without Premium the dashboard shows this banner, and every request fails with
HTTP 403.)

## Spotify's own audio (the `spotify` backend)

This one is on by default. It needs three things, each one-time or automatic:

**1. A Soloist API key — one per person, free for a Premium account.** Make it at
[developer.spotify.com/dashboard/soloist](https://developer.spotify.com/dashboard/soloist).
It is a secret, like a password: streamrip never prints it, logs it, or sends it
anywhere but Spotify. When streamrip asks, paste it in (it goes in
`soloist_api_key`, hidden as you type).

**2. A pairing, once, with a Spotify app on the same network.** The first time,
streamrip tells you to:

1. Open the Spotify app on a device on this network and press play,
2. Tap *Connect to a device*,
3. Pick the device named `device_name` (default `streamrip`) and play a track.

The session is stored on this machine and replayed by every later run — no more
pairing.

**3. Linux, and PipeWire or PulseAudio.** The recording is a sink monitor, which
is a Linux thing, so the `spotify` backend runs on Linux for now. On macOS or
Windows — or if any of the above is missing — streamrip does not fail: it falls
back to YouTube Music for that track, so you still get the file.

streamrip keeps its own copy of the Soloist binary (the official builds expire
after 90 days; when one does, streamrip quietly downloads a fresh one and carries
on). There is nothing else to install.

```bash
streamrip url https://open.spotify.com/album/<album id>
streamrip url https://open.spotify.com/playlist/<id of a playlist of yours> spotify:track:<track id>
streamrip search spotify track 'daft punk get lucky'
```

To go lossy on purpose (for example, to a device that wants MP3) set
`backend = "youtube"` in the `[spotify]` section.

## What works

What Spotify allows an app of this kind, and so what works:

- **Tracks, albums, artists and search** work for anyone's. Search gives at most
  50 results.
- **Playlists** only if you **own them or collaborate on them**: Spotify blocks
  the tracks of a playlist you merely follow, and Spotify's own editorial ones.
  Copy such a playlist into one of your own first.
- Podcasts and local files in a playlist are skipped.
- An app can be used by at most five people (the ones you add under *User
  Management*). That is Spotify's limit for apps in development mode.

## Options

All options are in the `[spotify]` section of `streamrip config open`:

- `backend` — `spotify` (the account's own audio, via Soloist) or `youtube`.
- `soloist_api_key` — the Soloist key, for the `spotify` backend.
- `device_name` — the name the pairing looks for (default `streamrip`).
- `audio_format` (`m4a` or `mp3`) and `audio_bitrate` — only for the `youtube`
  backend, where the audio has to be re-encoded; the `spotify` backend is FLAC.
- `match_videos` — when YouTube Music has no "song" that matches a track, accept
  an official video whose title, artist and length match (turn it off to be
  strict). `youtube` backend only.
- `redirect_uri` — the login callback, as above.

## Credits

The Spotify support in this fork borrows from three projects. Thank you:

- **[Music-Sync](https://opencommit.eu/Stensel8/Music-Sync)** (AGPL-3.0, the same
  author), which is part of streamrip now as `streamrip playlist`. The Spotify login and the handling of Spotify's API follow it: OAuth
  with PKCE and a loopback redirect (`streamrip/rip/spotify_login.py`,
  `streamrip/client/spotify.py`), token refresh, and the rules for apps in
  development mode since February 2026 (Premium for the app's owner, ten
  search results per request, playlist tracks under `item`, only your own
  playlists). The matching of a Spotify track with a YouTube Music result
  (`streamrip/client/audio_match.py`) is adapted from its `matching.py`, and the
  screenshots above are from its README.
- **[spotDL](https://github.com/spotDL/spotify-downloader)** (MIT). The idea of
  taking only the metadata from Spotify and the audio from YouTube Music, and of
  looking a track up by its ISRC first and then scoring candidates by name,
  artist, album and length (`streamrip/client/ytmusic.py`); also the loopback
  redirect on `127.0.0.1` for the login.
- **[MediaHarbor](https://github.com/MediaHarbor/mediaharbor)** (GPL-3.0). Read as
  a reference for how an app talks to Spotify's Web API (client credentials and
  user tokens, following `next` links to the end of a list, backing off when
  Spotify answers 429); no code is taken from it.
- **[Spotify Soloist](https://github.com/spotify/soloist)** (MIT). Spotify's own
  official headless client: a Connect device that plays a track and renders it to
  local audio at the account's quality. streamrip drives it and records the
  result (`streamrip/client/soloist.py`); it plays Spotify's own audio, it does
  not crack it.
