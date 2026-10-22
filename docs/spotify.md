[← Back to the README](../README.md)

# Spotify

Spotify encrypts its own audio, so streamrip does not download from Spotify.
What it does is what [spotDL](https://github.com/spotDL/spotify-downloader) does:
Spotify says what a track is (title, artists, album, track number, ISRC, cover,
and which tracks an album, artist or playlist has), streamrip finds the same
recording on **YouTube Music** (by its ISRC first, then by artist, title and
length) and downloads that, tagged with what Spotify says. Because of that:

- The audio is what YouTube Music has: AAC of about 128 kbps (`audio_format =
  "m4a"`, kept as it is) or MP3 at `audio_bitrate`. It is **not lossless**, and
  not Spotify's own quality. For lossless, use Qobuz, Tidal or Deezer.
- A track YouTube Music does not have, or has only as another version (live,
  remix, ...), is reported as not found rather than downloaded wrong.
- **For better quality, move the playlist first.** With
  [Music-Sync](https://github.com/Stensel8/Music-Sync) you can move your liked
  songs and playlists from Spotify to Tidal, or export them to a CSV, and then
  download them from a lossless source: [`streamrip csv`](csv-lists.md) searches
  such a CSV on Qobuz, Tidal or Deezer. streamrip reminds you of this the first
  time you use Spotify in a run.
- It needs [ffmpeg](https://ffmpeg.org) like every source, and yt-dlp, which
  wants a JavaScript runtime to read YouTube: install [Deno](https://deno.com)
  or [Node.js](https://nodejs.org) if a download fails saying so.

**Spotify's own rules for logging in** (since February 2026): there is no shared
login for outside programs, so you make a small app of your own in Spotify's
developer dashboard, and **the account that owns the app must have Spotify
Premium**. Without Premium Spotify refuses every request. If you have no Premium,
someone who does can make the app and add your Spotify account under *Settings >
User Management*. Making the app takes a minute:

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   log in and choose **Create app**. Name and description can be anything.
2. **Redirect URI**: `http://127.0.0.1:9900/callback`, character for character.
   It has to be `127.0.0.1`, not `localhost`; Spotify no longer accepts that.
3. Under *Which API/SDKs are you planning to use?* tick **Web API** only.

   ![Only Web API is ticked.](https://github.com/Stensel8/Music-Sync/blob/main/docs/images/spotify-web-api-ticked.avif?raw=true)

4. Save, open the app's *Settings* and copy its **Client ID**. You do not need
   the client secret.

Then run streamrip with any Spotify link. The first time it asks for the Client
ID and opens your browser to log in; it catches the answer itself on
`127.0.0.1:9900` (if streamrip runs on another machine, it offers to let you paste
the address you end up on instead). The login is saved in the `[spotify]`
section of the config and renews itself. If you had to give the Client ID by
hand, it goes in `client_id` there.

![What you see without Premium: "Your application is blocked from accessing the Web API since you do not have a Spotify Premium subscription."](https://github.com/Stensel8/Music-Sync/blob/main/docs/images/spotify-premium-required.avif?raw=true)

(Without Premium the dashboard shows this banner, and every request fails with
HTTP 403.)

```bash
streamrip url https://open.spotify.com/album/<album id>
streamrip url https://open.spotify.com/playlist/<id of a playlist of yours> spotify:track:<track id>
streamrip search spotify track 'daft punk get lucky'
```

What Spotify allows an app of this kind, and so what works:

- **Tracks, albums, artists and search** work for anyone's. Search gives at most
  50 results.
- **Playlists** only if you **own them or collaborate on them**: Spotify blocks
  the tracks of a playlist you merely follow, and Spotify's own editorial ones.
  Copy such a playlist into one of your own first.
- Podcasts and local files in a playlist are skipped.
- An app can be used by at most five people (the ones you add under *User
  Management*). That is Spotify's limit for apps in development mode.

All options are in the `[spotify]` section of `streamrip config open`:
`audio_format` (`m4a` or `mp3`), `audio_bitrate` (only when audio has to be
re-encoded), `match_videos` (accept an official video when YouTube Music has no
matching "song"; turn it off to be strict) and `redirect_uri`.

## Credits

The Spotify support in this fork borrows from three projects. Thank you:

- **[Music-Sync](https://github.com/Stensel8/Music-Sync)** (AGPL-3.0, the same
  author). The Spotify login and the handling of Spotify's API follow it: OAuth
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
  Spotify answers 429); no code is taken from it. Its way of downloading
  Spotify's own audio, by decrypting the Widevine-protected streams with a
  device file, is **not** used: streamrip does not decrypt Spotify's audio.
