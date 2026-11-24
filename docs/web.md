[← Back to the README](../README.md)

# The web interface

```bash
streamrip web
```

opens <http://127.0.0.1:8888> in your browser. It does what the commands do, on
a web page.

- **Download**: links (typed, or in a file), search with covers, an id, a CSV
  list or a last.fm playlist. A list can fall back to a second and third source
  for a track the first lacks. *Preview* looks everything up and says what would
  be downloaded, without downloading it. Downloads run in the background, and
  the page shows the tracks, the progress and the log. One runs at a time.
- **Playlists**: download your liked songs or a playlist from Spotify or Tidal
  from a source of your choice, transfer them to the other service, export them
  (CSV, or M3U8 for a player) and import CSV files. Only look (dry run) and
  the score a match needs are there too, and a finished job offers a CSV of how
  every track was matched (see [Playlists](playlists.md)).
- **Accounts**: every login: Qobuz, Tidal, Deezer, SoundCloud, and the apps for
  Spotify and Tidal's API. *Check all logins* asks every service whether its
  login works, and shows the plan. A login that waits can be dismissed.
- **Library**: what was downloaded and what failed. Retry the failures, or
  forget them (and what was downloaded, to download it again).

The command line does the same in the same sections, see `streamrip --help`.
Settings stay in `config.toml`, which `streamrip config open` opens.

Logins go into the same `state.toml` as the command line's, so a login made on
the page also works in the terminal, and the other way round.

The web interface is part of streamrip, nothing extra to install. Use `--port`
for another port and `--no-browser` to keep the browser closed.

## It's for you alone

The page has no password. That's why it only answers to `127.0.0.1` and
`localhost` (so another site can't reach it under a different name), refuses
changes started by other websites and only runs its own scripts. Never make it
reachable from a network.

## In a container

```bash
mkdir -p streamrip
podman run --rm -it -v ./streamrip:/data -v ~/Music:/data/Music \
  -p 127.0.0.1:8888:8888 \
  opencommit.eu/stensel8/streamrip web --host 0.0.0.0
```

Then open <http://127.0.0.1:8888>. Inside the container it has to listen on all
addresses (`--host 0.0.0.0`), so publish the port on `127.0.0.1` only, as above.
Spotify and Tidal logins can't find their way back into the container by
themselves. Paste the address your browser ends up on into the box under the
login link.
