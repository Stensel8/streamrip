[← Back to the README](../README.md)

# The web interface

```bash
streamrip web
```

opens <http://127.0.0.1:8888> in your browser. It does what the commands do, on
a web page.

- **Download**: paste links from any source (albums, tracks, playlists, artists,
  labels), search a source and tick what you want from the results, or download
  a CSV list. Downloads run in the background, and the page shows the tracks,
  the progress and the log. One download runs at a time; the next one waits.
- **Playlists**: download your liked songs or a playlist from Spotify or Tidal
  from a source of your choice, transfer them to the other service, or export
  and import CSV files (see [Playlists](playlists.md)).
- **Accounts**: log in to Qobuz, Tidal and Deezer for downloading, and set up
  the Spotify app (for downloads and playlists) and Tidal's API (for playlists).
  A browser login finds its way back to streamrip on this computer. If your
  browser is on another device, paste the address it ends up on.

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
