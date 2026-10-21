FROM python:3.14-slim

# Deno is the JavaScript runtime yt-dlp needs for Spotify.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /src
COPY pyproject.toml README.md ./
COPY streamrip ./streamrip
# The ffmpeg extra brings a static ffmpeg, so no system packages are needed.
RUN pip install --no-cache-dir ".[ffmpeg]" && rm -rf /src

# Config, databases and downloads all live under HOME.
ENV HOME=/data
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
WORKDIR /data
VOLUME /data

ENTRYPOINT ["streamrip"]
