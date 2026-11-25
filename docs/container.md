[← Back to the README](../README.md)

# Podman and Docker

The image has everything streamrip needs built in, including ffmpeg and the
JavaScript runtime for Spotify. It runs on amd64 and arm64 and is about 50 MiB
to pull. It's distroless, so there's no shell inside, only streamrip. Tags are
`latest` and every version, like `2.6.5`. The image is scanned for known
vulnerabilities on every change and once a week.

Every release is built from the same tag for both platforms and published as
`opencommit.eu/stensel8/streamrip` and `ghcr.io/stensel8/streamrip`. Use
whichever you like.

```bash
mkdir -p streamrip      # the folder must exist
podman run --rm -it -v ./streamrip:/data -v ~/Music:/data/Music \
  opencommit.eu/stensel8/streamrip url <link>
```

Everything after the image name is a streamrip command. Config and databases
live in `/data`, which is what the folder is for. Downloads go to `/data/Music`,
your own music folder. On a Linux desktop in another language,
`xdg-user-dir MUSIC` tells you its name. Without the second `-v`, downloads end
up in `./streamrip/Music`. `-it` is needed for the prompts, progress bars and
search menu. The config file is `./streamrip/.config/streamrip/config.toml`, and your logins
are in `state.toml` next to it.

- **Docker:** same command, but add `--user "$(id -u):$(id -g)"` after `run` so
  the files belong to you. Under rootless Podman they already do.
- **SELinux** (Fedora, RHEL): add `:Z` to the volume, as in
  `-v ./streamrip:/data:Z`.
- **Docker 23 or newer** is required. The image is compressed with zstd, which
  older versions can't unpack. Podman can.
- **Updating:** pull the latest image
  (`podman pull opencommit.eu/stensel8/streamrip`). The image doesn't check for
  updates itself.

To type it like the Python version:

```bash
alias streamrip='podman run --rm -it -v "$HOME/streamrip:/data" -v "$HOME/Music:/data/Music" opencommit.eu/stensel8/streamrip'
```

To build the image yourself from this checkout, run `podman build -t streamrip .`
(Docker: `docker build -f Containerfile -t streamrip .`).

## Logging in

A container has no screen and your browser can't reach it. Qobuz logs in with the
short script on the Accounts page of [the web interface](#the-web-interface) (it
works through the published port) or by hand, Deezer by hand, and for Spotify and
Tidal you paste the address. See [Logging in](logging-in.md).

Spotify's own audio needs a sound server, which a container does not have, so
there a Spotify track comes from YouTube Music ([Spotify](spotify.md)). Everything
else works: downloads, tags, ReplayGain, `[conversion]` to every codec, YouTube Music,
and the web interface.

## The web interface

The image includes [`streamrip web`](web.md) too. Only publish its port on
`127.0.0.1`:

```bash
podman run --rm -it -v ./streamrip:/data -v ~/Music:/data/Music \
  -p 127.0.0.1:8888:8888 opencommit.eu/stensel8/streamrip web --host 0.0.0.0
```

To have the container's health reported, add a check that asks `/health`:

```bash
  --health-cmd '["/opt/streamrip/bin/python","-c","import urllib.request as u; u.urlopen(\"http://127.0.0.1:8888/health\", timeout=3)"]'
```

The image has no check of its own: most of its runs are one command, with
nothing to listen on.
