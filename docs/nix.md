[← Back to the README](../README.md)

# Nix (x86_64 Linux)

With flakes enabled, run these commands from this checkout:

```bash
nix build "path:$PWD"                 # ./result/bin/streamrip
nix run "path:$PWD" -- --help
nix develop "path:$PWD"
nix flake check "path:$PWD"            # package, non-browser tests, offline CLI checks
```

The flake uses Python 3.15. nixpkgs does not cache Python 3.15 libraries yet, so
the first build compiles them (about an hour) and is fast afterwards. It reads
name/version and descriptive metadata from `pyproject.toml`, and
keeps an explicit Nix dependency list. The standard package
build validates declared runtime dependency constraints. `flake.lock` pins
nixpkgs; Poetry's `poetry.lock` is an
independent lock and is not used by Nix. The development shell supplies the same
Python dependency set plus build/dev tools, with Poetry as a standalone
convenience. It does not run `poetry install`: run `python -m pytest -m 'not
real_browser'` in the checkout to test working-tree imports.

The package and shell supply FFmpeg, Deno and Chromium on `PATH`, preserving
the caller's other tools. Chromium discovery does **not** establish that
Playwright can launch it or complete a live provider login; those require
separate interactive validation. Other systems are not currently supported.
