[← Back to the README](../README.md)

# Nix (x86_64 Linux)

With flakes enabled, run these from this checkout:

```bash
nix build .           # ./result/bin/streamrip
nix run . -- --help
nix develop .
nix flake check .     # package, non-browser tests, offline CLI checks
```

The flake uses Python 3.15 and, through
[uv2nix](https://github.com/pyproject-nix/uv2nix), the packages and versions in
`uv.lock`, installed from the same wheels pip uses. Nothing is compiled, and
nixpkgs' own Python packages aren't used. `flake.lock` pins nixpkgs and the
uv2nix inputs.

The development shell has streamrip installed in editable mode from the
checkout, with every extra, the dev tools and uv. Run
`pytest -m 'not real_browser and not login'` there to test the working tree.

The package and the shell add FFmpeg, Deno and Chromium to `PATH` and leave your
other tools alone. Chromium being found doesn't prove that Playwright can launch
it or log in to a real service; test that by hand. Only x86_64 Linux is
supported for now.
