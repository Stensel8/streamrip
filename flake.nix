{
  description = "Streamrip package and development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, pyproject-nix, uv2nix, pyproject-build-systems, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      inherit (pkgs) lib;
      project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;

      # The packages and versions of uv.lock, installed from the same wheels pip
      # takes: nothing is compiled, and nixpkgs' Python libraries are not used.
      workspace = uv2nix.lib.workspace.loadWorkspace {
        # Only what the package is built from: a .venv, caches or docs next to
        # it neither go into the store nor change what is built.
        workspaceRoot = lib.fileset.toSource {
          root = ./.;
          fileset = lib.fileset.unions
            [ ./pyproject.toml ./uv.lock ./README.md ./LICENSE ./streamrip ];
        };
      };
      pythonSet =
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python315; })
        .overrideScope (lib.composeManyExtensions [
          pyproject-build-systems.overlays.wheel
          (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
          (final: prev: {
            # The Node.js in Playwright's wheel crashes once it is patched to run
            # on Nix, so its driver runs on nixpkgs' Node.js.
            playwright = prev.playwright.overrideAttrs (old: {
              postInstall = (old.postInstall or "") + ''
                ln -sf ${lib.getExe pkgs.nodejs} \
                  "$out/${final.python.sitePackages}/playwright/driver/node"
              '';
            });
          })
        ]);
      inherit (pythonSet.streamrip) version;

      # The browser extra drives the Chromium below for the Qobuz and Deezer logins.
      venv = pythonSet.mkVirtualEnv "streamrip-env" { streamrip = [ "browser" ]; };
      runtimeTools = [ pkgs.ffmpeg-headless pkgs.deno pkgs.chromium ];

      streamrip = pkgs.runCommand "streamrip-${version}" {
        nativeBuildInputs = [ pkgs.makeWrapper ];
        meta = {
          inherit (project) description;
          homepage = project.urls.Homepage;
          license = lib.licenses.gpl3Only;
          mainProgram = "streamrip";
          platforms = [ system ];
        };
      } ''
        makeWrapper ${venv}/bin/streamrip $out/bin/streamrip \
          --prefix PATH : ${lib.makeBinPath runtimeTools}
      '';

      # Every extra and the dev group: pytest and the rest.
      testEnv = pythonSet.mkVirtualEnv "streamrip-test-env" workspace.deps.all;
      tests = pkgs.runCommand "streamrip-${version}-tests" {
        src = lib.fileset.toSource {
          root = ./.;
          fileset = lib.fileset.unions [ ./pyproject.toml ./streamrip ./tests ];
        };
        nativeBuildInputs = [ testEnv ] ++ runtimeTools;
      } ''
        cp -r "$src" source && chmod -R u+w source && cd source
        export HOME="$TMPDIR/home"
        pytest -p no:cacheprovider -m "not real_browser"
        touch "$out"
      '';
      smoke = pkgs.runCommand "streamrip-${version}-smoke" { } ''
        export HOME="$TMPDIR/home"
        # A config is the packaged template with one option set; every option
        # of it is required, so the template is the smallest valid file.
        ${venv}/bin/python - "$TMPDIR/smoke.toml" <<'EOF'
        import sys
        from pathlib import Path
        from streamrip.config import BLANK_CONFIG_PATH, Config

        path = Path(sys.argv[1])
        path.write_text(
            BLANK_CONFIG_PATH.read_text().replace("[cli]\n", "[cli]\nno_update_check = true\n", 1)
        )
        assert Config(path).session.cli.no_update_check
        EOF
        ${lib.getExe streamrip} --config-path "$TMPDIR/smoke.toml" --version
        ${lib.getExe streamrip} --config-path "$TMPDIR/smoke.toml" --help
        touch "$out"
      '';

      # streamrip installed editable from the checkout, so the shell runs the
      # working tree.
      # (The editable overlay needs the checkout as a path, not the filtered
      # source above.)
      editableSet = pythonSet.overrideScope
        ((uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; })
          .mkEditablePyprojectOverlay { root = "$REPO_ROOT"; });
      devEnv = editableSet.mkVirtualEnv "streamrip-dev-env" workspace.deps.all;
    in {
      packages.${system} = { default = streamrip; inherit streamrip; };
      checks.${system} = { inherit streamrip tests smoke; };
      devShells.${system}.default = pkgs.mkShell {
        packages = [ devEnv pkgs.uv ] ++ runtimeTools;
        env = {
          UV_NO_SYNC = "1";
          UV_PYTHON = editableSet.python.interpreter;
          UV_PYTHON_DOWNLOADS = "never";
        };
        shellHook = ''
          unset PYTHONPATH
          export REPO_ROOT=$(git rev-parse --show-toplevel)
        '';
      };
    };
}
