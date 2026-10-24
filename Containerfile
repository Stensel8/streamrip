# Fully qualified names, so Podman never has to ask which registry is meant.
FROM docker.io/library/python:3.15-slim@sha256:533b0b2f9b5d46fd097091cf826f27902f1d6c3bca714b6dc74551356aa97a51 AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv
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


FROM docker.io/library/python:3.15-slim@sha256:533b0b2f9b5d46fd097091cf826f27902f1d6c3bca714b6dc74551356aa97a51

LABEL org.opencontainers.image.title="streamrip" \
      org.opencontainers.image.description="A fast, all-in-one scriptable music downloader for Qobuz, Deezer, Tidal, SoundCloud, and Spotify." \
      org.opencontainers.image.source="https://github.com/Stensel8/streamrip" \
      org.opencontainers.image.licenses="GPL-3.0-only"

# Deno is the JavaScript runtime yt-dlp needs for Spotify.
COPY --from=docker.io/denoland/deno:bin-2.9.8@sha256:b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543 /deno /usr/local/bin/deno
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
# brings its own, older copies of certifi, urllib3 and msgpack. Removed rather
# than uninstalled: running pip would leave a layer of bytecode behind.
RUN rm -r /usr/local/lib/python3.15/site-packages/pip* /usr/local/bin/pip*
WORKDIR /data
VOLUME /data

ENTRYPOINT ["streamrip"]
