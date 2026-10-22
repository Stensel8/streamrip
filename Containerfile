# Fully qualified names, so Podman never has to ask which registry is meant.
FROM docker.io/library/python:3.15-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY streamrip ./streamrip
# Every dependency has a wheel for this Python, so no compiler is needed. The
# ffmpeg extra brings a static ffmpeg.
RUN pip install --no-cache-dir --prefix=/install ".[ffmpeg]"


FROM docker.io/library/python:3.15-slim

LABEL org.opencontainers.image.title="streamrip" \
      org.opencontainers.image.description="A fast, all-in-one scriptable music downloader for Qobuz, Deezer, Tidal, SoundCloud, and Spotify." \
      org.opencontainers.image.source="https://github.com/Stensel8/streamrip" \
      org.opencontainers.image.licenses="GPL-3.0-only"

# Deno is the JavaScript runtime yt-dlp needs for Spotify.
COPY --from=docker.io/denoland/deno:bin /deno /usr/local/bin/deno
COPY --from=build /install /usr/local

# Config, databases and downloads all live under HOME. There is no USER: under
# rootless Podman, root in the container is whoever started it, so the files in
# /data are theirs without any flags. Docker users add `--user "$(id -u):$(id -g)"`.
ENV HOME=/data
# A container is updated by pulling a newer image, not with the pip command the
# update notice would give, and the check costs about half a second of every run.
ENV STREAMRIP_NO_UPDATE_CHECK=1
# pip only installs, and the image is updated by pulling a newer one. It also
# brings its own, older copies of certifi, urllib3 and msgpack.
RUN python -m pip uninstall --yes --root-user-action=ignore pip && mkdir /data
WORKDIR /data
VOLUME /data

ENTRYPOINT ["streamrip"]
