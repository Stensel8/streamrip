FROM python:3.15 AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY streamrip ./streamrip
# The full image has a compiler, for dependencies without a wheel for this
# Python yet. The ffmpeg extra brings a static ffmpeg.
RUN pip install --no-cache-dir --prefix=/install ".[ffmpeg]"


FROM python:3.15-slim

# Deno is the JavaScript runtime yt-dlp needs for Spotify.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
COPY --from=build /install /usr/local

# Config, databases and downloads all live under HOME.
ENV HOME=/data
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
WORKDIR /data
VOLUME /data

ENTRYPOINT ["streamrip"]
