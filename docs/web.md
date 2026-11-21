[← Back to the README](../README.md)

# The web interface

```bash
streamrip web
```

opens <http://127.0.0.1:8888> in your browser: what the commands do, on a page.

- **Download**: paste links (albums, tracks, playlists, artists, labels, from
  every source); search a source, see the covers and tick what to download; or
  download a CSV list. A download runs in the background, with its tracks, how
  far it is and its log on the page. One runs at a time; the next waits for it.
- **Playlists**: download your Spotify or Tidal liked songs or a playlist from
  the source you pick, transfer them to the other service, export them to CSV
  or import a CSV (see [Playlists](playlists.md)).
- **Accounts**: log in to Qobuz, Tidal and Deezer for downloading, set up the
  Spotify app both downloads and playlists use, and Tidal's API for
  playlists. A browser login comes back to streamrip on this computer by
  itself; when your browser is on another device, paste the address it ends up
  on instead.

Everything is kept in the same `config.toml` as the command line's, so a login
made on the page works in the terminal and the other way round.

It needs the `web` extra (Flask). The installers, the container image and the
Nix flake include it; with a wheel:
`uv tool install "./streamrip-*.whl[web]"`. `--port` picks another port,
`--no-browser` leaves the browser closed.

## It is for you alone

The page has no password, so it only answers to `127.0.0.1` and `localhost`
(a page on another site cannot reach it under another name), refuses changes
that another website starts, and runs only its own scripts. Never make it
reachable from a network.

## In a container

```bash
mkdir -p streamrip
podman run --rm -it -v ./streamrip:/data -v ~/Music:/data/Music \
  -p 127.0.0.1:8888:8888 \
  opencommit.eu/stensel8/streamrip web --host 0.0.0.0
```

and open <http://127.0.0.1:8888>. Inside the container it has to listen on
every address (`--host 0.0.0.0`), so publish the port on `127.0.0.1` only, as
above. A Spotify or Tidal login cannot come back into the container by itself:
paste the address your browser ends up on, in the box under the login link.
