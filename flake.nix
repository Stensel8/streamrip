{
  description = "Streamrip package and development environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      # Python 3.15 is not in the nixpkgs binary cache yet (only its default
      # Python is), so the libraries are built here. Skip their own test suites
      # and import checks (which need test-only modules): that is a few hundred
      # builds fewer. streamrip's own tests still run.
      pkgs = import nixpkgs {
        inherit system;
        overlays = [
          (final: prev: {
            pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
              (pyFinal: pyPrev:
                nixpkgs.lib.optionalAttrs (pyPrev.python.pythonVersion == "3.15")
                  (builtins.mapAttrs (_: pkg:
                    let r = builtins.tryEval pkg;
                    in if r.success && r.value ? overridePythonAttrs
                       then r.value.overridePythonAttrs {
                         doCheck = false;
                         pythonImportsCheck = [ ];
                       }
                       else pkg) pyPrev))
            ];
          })
        ];
      };
      inherit (pkgs) lib;
      python = pkgs.python315;
      pythonPackages = python.pkgs;
      project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;

      # Keep this explicit list in sync with pyproject.toml, whose `browser`
      # extra (playwright) is part of it: Nix has Chromium and a screen to use.
      runtimeDependencies = with pythonPackages; [
        mutagen tomlkit pathvalidate textual textual-image pillow deezer-py
        pycryptodomex m3u8 aiofiles aiohttp aiolimiter rich click
        requests playwright ytmusicapi yt-dlp yt-dlp-ejs websockets certifi
      ];
      buildDependencies = [ pythonPackages.poetry-core ];
      devDependencies = with pythonPackages; [ pytest pytest-mock pytest-asyncio ];
      developmentPython = python.withPackages (_:
        runtimeDependencies ++ buildDependencies ++ devDependencies);
      runtimeTools = [ pkgs.ffmpeg-headless pkgs.deno pkgs.chromium ];
      mainProgram = "streamrip";

      streamrip = pythonPackages.buildPythonApplication {
        pname = project.name;
        inherit (project) version;
        src = lib.cleanSource self;
        pyproject = true;
        build-system = buildDependencies;
        dependencies = runtimeDependencies;
        # Temporary: nixpkgs still has Pillow 12, pyproject.toml wants 13. See #69.
        # Likewise certifi: pyproject.toml wants a newer CA bundle than nixpkgs has.
        # yt-dlp and yt-dlp-ejs are pinned exactly there, and nixpkgs has whichever
        # pair it has: they only need to match each other.
        pythonRelaxDeps = [ "pillow" "certifi" "yt-dlp" "yt-dlp-ejs" ];
        nativeBuildInputs = [ pkgs.makeWrapper ];
        nativeCheckInputs = [ pythonPackages.pytestCheckHook ] ++ devDependencies ++ runtimeTools;
        doCheck = true;
        disabledTestMarks = [ "real_browser" ];
        pythonImportsCheck = [ "streamrip" ];

        postPatch = ''
          export HOME="$TMPDIR/streamrip-home"
          export XDG_CONFIG_HOME="$HOME/.config"
          export XDG_CACHE_HOME="$HOME/.cache"
          export XDG_DATA_HOME="$HOME/.local/share"
          mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
        '';
        postFixup = ''
          wrapProgram "$out/bin/${mainProgram}" \
            --prefix PATH : ${lib.makeBinPath runtimeTools}
        '';
        doInstallCheck = true;
        postInstallCheck = ''
          # A config is the packaged template with one option set; every option
          # of it is required, so the template is the smallest valid file.
          ${developmentPython}/bin/python - "$TMPDIR/smoke.toml" <<'EOF'
          import sys
          from pathlib import Path
          from streamrip.config import BLANK_CONFIG_PATH, Config

          path = Path(sys.argv[1])
          path.write_text(
              BLANK_CONFIG_PATH.read_text().replace("[cli]\n", "[cli]\nno_update_check = true\n", 1)
          )
          assert Config(path).session.cli.no_update_check
          EOF
          "$out/bin/${mainProgram}" --config-path "$TMPDIR/smoke.toml" --version
          "$out/bin/${mainProgram}" --config-path "$TMPDIR/smoke.toml" --help
        '';
        meta = {
          inherit (project) description;
          homepage = project.urls.Homepage;
          license = lib.licenses.gpl3Only;
          inherit mainProgram;
          platforms = [ system ];
        };
      };
    in {
      packages.${system} = { default = streamrip; inherit streamrip; };
      checks.${system}.default = streamrip;
      devShells.${system}.default = pkgs.mkShell {
        packages = [ developmentPython pkgs.poetry pkgs.ruff ] ++ runtimeTools;
        # No installation of the application: tests import the working tree.
      };
    };
}
