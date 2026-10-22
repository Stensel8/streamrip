# Fully qualified names, so Podman never has to ask which registry is meant.
FROM docker.io/library/python:3.15-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
# The versions of uv.lock, all from wheels: no compiler is needed. The ffmpeg
# extra brings a static ffmpeg. streamrip itself is copied into the
# environment (--no-editable), which is all the image takes from this stage.
ENV UV_PROJECT_ENVIRONMENT=/opt/streamrip \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --extra ffmpeg
COPY README.md LICENSE ./
COPY streamrip ./streamrip
RUN uv sync --locked --no-dev --no-editable --extra ffmpeg


FROM docker.io/library/python:3.15-slim

LABEL org.opencontainers.image.title="streamrip" \
      org.opencontainers.image.description="A fast, all-in-one scriptable music downloader for Qobuz, Deezer, Tidal, SoundCloud, and Spotify." \
      org.opencontainers.image.source="https://github.com/Stensel8/streamrip" \
      org.opencontainers.image.licenses="GPL-3.0-only"

# Deno is the JavaScript runtime yt-dlp needs for Spotify.
COPY --from=docker.io/denoland/deno:bin /deno /usr/local/bin/deno
COPY --from=build /opt/streamrip /opt/streamrip
ENV PATH=/opt/streamrip/bin:$PATH

# Config, databases and downloads all live under HOME. There is no USER: under
# rootless Podman, root in the container is whoever started it, so the files in
# /data are theirs without any flags. Docker users add `--user "$(id -u):$(id -g)"`.
ENV HOME=/data
# A container is updated by pulling a newer image, not with the pip command the
# update notice would give, and the check costs about half a second of every run.
ENV STREAMRIP_NO_UPDATE_CHECK=1
# pip only installs, and the image is updated by pulling a newer one. It also
# brings its own, older copies of certifi, urllib3 and msgpack.
RUN /usr/local/bin/python -m pip uninstall --yes --root-user-action=ignore pip && mkdir /data
WORKDIR /data
VOLUME /data

ENTRYPOINT ["streamrip"]
