{
  description = "Streamrip package and development environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      inherit (pkgs) lib;
      python = pkgs.python314;
      pythonPackages = python.pkgs;
      project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;

      # Keep this explicit list in sync with pyproject.toml.
      runtimeDependencies = with pythonPackages; [
        mutagen tomlkit pathvalidate textual textual-image pillow deezer-py
        pycryptodomex m3u8 aiofiles aiohttp aiolimiter rich click click-help-colors
        requests playwright ytmusicapi yt-dlp
      ] ++ pythonPackages.yt-dlp.optional-dependencies.default;
      buildDependencies = [ pythonPackages.poetry-core ];
      devDependencies = with pythonPackages; [ pytest pytest-mock pytest-asyncio certifi ];
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
          # A present [cli] must contain its required typed fields. All other
          # sections are filled from the upstream template by Config itself.
          cat > "$TMPDIR/smoke.toml" <<'EOF'
          [cli]
          progress_bars = false
          max_search_results = 10
          no_update_check = true
          EOF
          ${developmentPython}/bin/python -c \
            'import sys; from streamrip.config import Config; assert Config(sys.argv[1]).session.cli.no_update_check' \
            "$TMPDIR/smoke.toml"
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
