[← Back to the README](../README.md)

# Nix (x86_64 Linux)

With flakes enabled, run these commands from this checkout:

```bash
nix build .           # ./result/bin/streamrip
nix run . -- --help
nix develop .
nix flake check .     # package, non-browser tests, offline CLI checks
```

The flake uses Python 3.15 and, through
[uv2nix](https://github.com/pyproject-nix/uv2nix), the packages and versions of
`uv.lock`, installed from the same wheels pip takes: nothing is compiled, and
nixpkgs' own Python libraries are not used. `flake.lock` pins nixpkgs and the
uv2nix inputs. The development shell has streamrip installed editable from the
checkout, every extra, the dev tools and uv: `pytest -m 'not real_browser and not login'`
tests the working tree.

The package and shell supply FFmpeg, Deno and Chromium on `PATH`, preserving
the caller's other tools. Chromium discovery does **not** establish that
Playwright can launch it or complete a live provider login; those require
separate interactive validation. Other systems are not currently supported.
