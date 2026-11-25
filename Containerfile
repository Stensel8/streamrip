# --- tools: an ffmpeg with only what streamrip uses, and QuickJS-ng for yt-dlp ---
FROM docker.io/library/debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0 AS tools

# Patchbot keeps these four current: a tag of FFmpeg's own git and its commit,
# and QuickJS-ng's release files with their sha256.
ARG FFMPEG_VERSION=n9.1
ARG FFMPEG_COMMIT=74e4393595cf2b5857f0da925c4e0dd74ea306e5
ARG QJS_AMD64=v0.18.0@sha256:85ded23b9fabe8c1644fc40a8d2bbdd71157affcbd807359c618cc717ee24d7d
ARG QJS_ARM64=v0.18.0@sha256:2c005c7171424a598d31c53c8dc4a0ae2e4958eb6fbb37908c885a307c78187c

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        ca-certificates curl git build-essential pkg-config nasm \
        libmp3lame-dev libopus-dev libvorbis-dev

WORKDIR /tmp/ffmpeg
RUN set -eux; \
    # FFmpeg's own git first, its GitHub mirror if that can't be reached: the
    # commit check makes either as good.
    timeout 30 git clone --quiet --depth 1 --branch "${FFMPEG_VERSION}" https://git.ffmpeg.org/ffmpeg.git . \
      || { rm -rf ./* ./.git; git clone --quiet --depth 1 --branch "${FFMPEG_VERSION}" https://github.com/FFmpeg/FFmpeg.git .; }; \
    test "$(git rev-parse HEAD)" = "${FFMPEG_COMMIT}"; \
    ./configure \
        --disable-everything --disable-autodetect --disable-network --disable-doc --disable-debug \
        --disable-ffplay --disable-ffprobe --disable-avdevice \
        --enable-small --enable-static --disable-shared \
        --pkg-config-flags=--static --extra-ldflags=-static \
        --enable-libmp3lame --enable-libopus --enable-libvorbis \
        --enable-protocol=file,pipe,concat \
        --enable-demuxer=flac,mp3,mov,matroska,ogg,aac,aiff,wav \
        --enable-muxer=flac,mp3,ipod,mp4,mov,ogg,opus,aiff,wav,null \
        --enable-decoder=flac,alac,aac,mp3float,mp3,opus,vorbis,mjpeg,png,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_s16be,pcm_s24be,pcm_s32be \
        --enable-encoder=flac,alac,aac,libmp3lame,libopus,libvorbis,pcm_s16be,pcm_s24be,pcm_s32be \
        --enable-parser=flac,aac,mpegaudio,opus,vorbis,mjpeg,png \
        --enable-bsf=aac_adtstoasc \
        --enable-filter=aformat,aresample,anull,null,format; \
    make -j"$(nproc)" ffmpeg; \
    install -m 755 ffmpeg /usr/local/bin/ffmpeg

RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) ref="${QJS_AMD64}" file=qjs-linux-x86_64 ;; \
        arm64) ref="${QJS_ARM64}" file=qjs-linux-aarch64 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/qjs "https://github.com/quickjs-ng/quickjs/releases/download/${ref%@*}/${file}"; \
    echo "${ref#*sha256:}  /usr/local/bin/qjs" | sha256sum -c -; \
    chmod 755 /usr/local/bin/qjs


# --- build: Python from uv and streamrip with its packages ---
FROM docker.io/library/debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0 AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d /uv /usr/local/bin/uv

# strip, for the packages below. A layer of its own: a change to streamrip's
# code doesn't fetch Debian's package lists again.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends binutils

ENV UV_PROJECT_ENVIRONMENT=/opt/streamrip \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_INSTALL_BIN=0 \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_CACHE_DIR=/root/.cache/uv/python \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /src
COPY pyproject.toml uv.lock ./
# The packages, then Python without Tk, IDLE, its tests and pip (20 MiB less),
# and the packages without their debug symbols: all in the layer that only
# pyproject.toml and uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    uv sync --locked --no-dev --no-install-project --python 3.15; \
    cd "$(dirname "$(dirname "$(readlink -f /opt/streamrip/bin/python)")")"; \
    rm -rf include share bin/pip* bin/idle* bin/pydoc* bin/*-config \
        lib/pkgconfig lib/*.a lib/libpython* lib/*tcl* lib/*tk* lib/itcl* lib/thread*; \
    cd lib/python3.*; \
    rm -rf test idlelib tkinter turtledemo turtle.py ensurepip pydoc_data config-* \
        site-packages/pip* site-packages/setuptools*; \
    # What streamrip never loads: a self-test, Pillow's AVIF, font, colour
    # profile and Tk modules with the libraries only they use, Playwright's
    # browser driver (there is no screen to log in on) and imageio-ffmpeg's
    # ffmpeg (the image has its own).
    cd /opt/streamrip/lib/python3.*/site-packages; \
    rm -rf playwright/driver imageio_ffmpeg/binaries; \
    rm -rf Cryptodome/SelfTest PIL/_avif.* PIL/_imagingcms.* PIL/_imagingft.* PIL/_imagingtk.* \
        pillow.libs/libavif* pillow.libs/liblcms2* pillow.libs/libfreetype* \
        pillow.libs/libharfbuzz* pillow.libs/libpng16* pillow.libs/libbrotli*; \
    find /opt/streamrip -type f \( -name '*.so' -o -name '*.so.*' \) -exec strip --strip-unneeded {} +
COPY README.md LICENSE ./
COPY streamrip ./streamrip
# Reinstalled every time: uv would take streamrip's wheel from the cache as
# long as pyproject.toml is unchanged, however much of the code changed.
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    uv sync --locked --no-dev --no-editable --reinstall-package streamrip --python 3.15; \
    /opt/streamrip/bin/python -c 'import ssl, sqlite3, lzma, bz2, ctypes, streamrip.rip.cli, streamrip.web; \
        from PIL import features; assert all(map(features.check, ("jpg", "zlib", "webp")))'; \
    /opt/streamrip/bin/streamrip --version


# --- the image: distroless, so no shell and no apt; Patchbot keeps the digest current ---
FROM gcr.io/distroless/cc-debian13:latest@sha256:dac90a25ffe03ebaace90cda886b081e61ab6cd77ac47eda51f7bcc15c320765

LABEL org.opencontainers.image.title="streamrip" \
      org.opencontainers.image.description="Keep the music you pay for. Take your playlists with you. streamrip downloads from Qobuz, Tidal, Deezer, SoundCloud and Spotify, and moves your playlists between Spotify, Tidal and CSV." \
      org.opencontainers.image.source="https://github.com/Stensel8/streamrip" \
      org.opencontainers.image.licenses="AGPL-3.0-only AND GPL-3.0-only"

COPY --from=build /opt/python /opt/python
COPY --from=tools /usr/local/bin/ffmpeg /usr/local/bin/qjs /usr/local/bin/
COPY --from=build /opt/streamrip /opt/streamrip

# No USER: under rootless Podman, root in the container is whoever started it,
# so the files in /data are theirs. Docker users add `--user "$(id -u):$(id -g)"`.
# Textual shows 16 colors to a TERM=xterm without COLORTERM, which is what a
# container gets; nearly every terminal today has 24-bit color.
ENV PATH=/opt/streamrip/bin:$PATH \
    COLORTERM=truecolor \
    HOME=/data \
    STREAMRIP_NO_UPDATE_CHECK=1
WORKDIR /data
VOLUME /data
# streamrip web --host 0.0.0.0, published on the host's 127.0.0.1 only.
EXPOSE 8888

ENTRYPOINT ["streamrip"]
