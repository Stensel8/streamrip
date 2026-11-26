#!/usr/bin/env bash
# Removes streamrip and what it keeps on this computer (Linux and macOS): the
# program, its config, logins and databases, and Spotify's client with its
# pairing. Your music stays where it is, and so do uv and Python. Asks first;
# -y removes streamrip's own without asking (and leaves what others share).
set -euo pipefail

yes=${1:-}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
cache=${XDG_CACHE_HOME:-$HOME/.cache}
if [ "$(uname -s)" = Darwin ]; then
    config="$HOME/Library/Application Support/streamrip"
    browser="$HOME/Library/Caches/ms-playwright"
else
    config=${XDG_CONFIG_HOME:-$HOME/.config}/streamrip
    browser="$cache/ms-playwright"
fi

ask() { read -r -p "$1 [y/N] " answer; [[ $answer == [yY]* ]]; }

music=$(sed -n 's/^folder = "\(.*\)"$/\1/p' "$config/config.toml" 2>/dev/null || true)
paths=() tools=()
for path in "$script_dir/.venv" "$config" "$data/streamrip" "$data/soloist" "$cache/soloist"; do
    if [ -e "$path" ]; then paths+=("$path"); fi
done
if command -v uv >/dev/null && uv tool list 2>/dev/null | grep -q '^streamrip '; then tools+=(uv); fi
if command -v pipx >/dev/null && pipx list --short 2>/dev/null | grep -q '^streamrip '; then tools+=(pipx); fi

# ${a[@]+"${a[@]}"}: an empty array, without macOS's bash 3.2 calling it unset.
if [ ${#paths[@]} -eq 0 ] && [ ${#tools[@]} -eq 0 ]; then
    echo "streamrip has nothing on this computer."
else
    echo "This removes:"
    for path in ${paths[@]+"${paths[@]}"}; do echo "  $path"; done
    for tool in ${tools[@]+"${tools[@]}"}; do echo "  streamrip, installed with $tool"; done
    if [ "$yes" = -y ] || ask "Go ahead?"; then
        # A server in the background (streamrip web --background) stops first.
        for bin in "$script_dir/.venv/bin/streamrip" "$(command -v streamrip || true)"; do
            if [ -x "$bin" ] && "$bin" web --stop >/dev/null 2>&1; then break; fi
        done
        # The sink Spotify's own audio plays into, while PulseAudio or PipeWire runs.
        if command -v pactl >/dev/null; then
            for module in $(pactl list short modules 2>/dev/null | awk '/sink_name=soloist_cap/ {print $1}' || true); do
                pactl unload-module "$module" || true
            done
        fi
        for tool in ${tools[@]+"${tools[@]}"}; do
            if [ "$tool" = uv ]; then uv tool uninstall streamrip; else pipx uninstall streamrip; fi >/dev/null
        done
        for path in ${paths[@]+"${paths[@]}"}; do rm -rf -- "$path"; done
        echo "Removed."
    fi
fi

# Shared with other programs: only when asked, never with -y.
if [ "$yes" != -y ]; then
    if [ -d "$browser" ] && ask "Also remove Playwright's browsers in $browser (other programs may use them)?"; then
        rm -rf -- "$browser"
    fi
    for engine in podman docker; do
        command -v "$engine" >/dev/null || continue
        images=$("$engine" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i '/streamrip:' || true)
        if [ -n "$images" ] && ask "Also remove these $engine images: $(echo $images)?"; then
            echo "$images" | xargs "$engine" rmi >/dev/null
        fi
    done
fi

echo "Your music is still in ${music:-your Music folder}; uv and Python stay too."
echo "This folder ($script_dir) you can delete yourself."
