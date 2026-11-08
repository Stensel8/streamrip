[← Back to the README](../README.md)

# Podman and Docker

The image has everything streamrip needs built in: ffmpeg, and the JavaScript
runtime Spotify needs. It runs on amd64 and arm64, and is about 65 MiB to pull.
It is distroless: there is no shell in it, only streamrip. Tags: `latest` and every
version, like `2.6.5`. It is scanned for known vulnerabilities on every change
and every week.

Each release is built from the same tag on both platforms, for
`opencommit.eu/stensel8/streamrip` and `ghcr.io/stensel8/streamrip`: either
works the same.

```bash
mkdir -p streamrip      # the folder must exist
podman run --rm -it -v ./streamrip:/data -v "$(xdg-user-dir MUSIC)":/data/Music \
  opencommit.eu/stensel8/streamrip url <link>
```

Everything after the image name is a streamrip command. Config and databases
live in `/data`, hence the folder, and downloads in `/data/Music`: your own music
folder, which `xdg-user-dir` finds on Linux (on macOS: `-v ~/Music:/data/Music`).
Without that second `-v` they end up in `./streamrip/Music`; `-it` is for the prompts,
progress bars and search menu. The config is
`./streamrip/.config/streamrip/config.toml`.

- **Docker:** the same, with `--user "$(id -u):$(id -g)"` after `run`, so the
  files are yours. Under rootless Podman they already are.
- **SELinux** (Fedora, RHEL): add `:Z` to the volume: `-v ./streamrip:/data:Z`.
- **Updating:** pull the newest image (`podman pull opencommit.eu/stensel8/streamrip`);
  the image does not check for updates itself.

To type it like the Python version:

```bash
alias streamrip='podman run --rm -it -v "$HOME/streamrip:/data" -v "$(xdg-user-dir MUSIC):/data/Music" opencommit.eu/stensel8/streamrip'
```

Build the image yourself from this checkout with `podman build -t streamrip .`
(Docker: `docker build -f Containerfile -t streamrip .`).

## Logging in

A container has no screen, and your browser can't reach it: Qobuz and Deezer
ask for the login by hand, and for Spotify you paste the address. See
[Logging in](logging-in.md).
