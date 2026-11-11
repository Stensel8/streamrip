"""Everything CI checks, and everything it cannot, run by hand. You need no CI to test streamrip completely.

    python checks/run.py offline    CI's job: lint, then the tests that need no login
    python checks/run.py login      the tests about logins, tokens and sessions (CI leaves these out)
    python checks/run.py live [SOURCE ...] [--config FILE] [--query TEXT]
                                    real accounts: streamrip asks you to log in, then tests itself
    python checks/run.py install    CI's installer job, on a copy of this checkout
    python checks/run.py container  CI's image build and smoke test (podman or docker)
    python checks/run.py nix        CI's `nix flake check` (it only sees files git tracks)
    python checks/run.py all        all of it

Any python and any folder will do: the script runs itself in the project's environment with uv.
`live` logs in to a real account of each SOURCE (qobuz, tidal, deezer, spotify, soundcloud; default
all) in a scratch config, so your own config and downloads are never touched. With `--config FILE`
it starts from the logins in that file instead of asking. `--query` is what is searched for (a
track that exists on the service). See checks/README.md.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UV = ["uv", "run", "--locked", "--all-extras"]
PYTEST = [*UV, "pytest"]
SOURCES = ("tidal", "qobuz", "deezer", "spotify", "soundcloud")
# SoundCloud only lets streamrip download what the artists themselves upload, so it is searched for that.
QUERIES = {"soundcloud": "lofi"}
QUERY = "Linkin Park In the End"
AUDIO = {".flac", ".mp3", ".m4a", ".opus", ".ogg"}

report: list[tuple[str, str]] = []


# Logs in to a source with the logins of the config at argv[1], without asking
# for any: exit code 2 when it has none, 3 when they no longer work.
LOGIN_CHECK = """
import asyncio, sys
from pathlib import Path
from streamrip.config import Config
from streamrip.exceptions import AuthenticationError, MissingCredentialsError
from streamrip.rip.main import Main
from streamrip.rip.prompter import get_prompter

async def check(path, source):
    config = Config(Path(path))
    async with Main(config) as main:
        if not get_prompter(main.clients[source], config).has_creds():
            print("no login for it in the config")
            return 2
        try:
            await main.get_logged_in_client(source)
        except (AuthenticationError, MissingCredentialsError) as e:
            reason = e.__cause__ or e
            print(f"its login no longer works ({reason}); log in again, paste the new config")
            return 3
    config.save_file()
    return 0

sys.exit(asyncio.run(check(*sys.argv[1:])))
"""


def record(name: str, problem: str | None = None, skipped: str | None = None) -> bool:
    """Note how one check went, and say so."""
    outcome = (
        f"skipped: {skipped}" if skipped else f"FAILED: {problem}" if problem else "ok"
    )
    report.append((name, outcome))
    print(f"-- {name}: {outcome}")
    return not problem and not skipped


def command(name: str, args: list, cwd: Path = ROOT) -> bool:
    """A check whose exit code is the verdict."""
    print(f"\n$ {' '.join(map(str, args))}", flush=True)
    code = subprocess.run(args, cwd=cwd, check=False).returncode
    return record(name, f"exit code {code}" if code else None)


def offline():
    lint = ["uv", "run", "--locked", "--only-group", "dev", "ruff"]
    command("lint", [*lint, "check", "."])
    command("format", [*lint, "format", "--check", "."])
    command("tests without logins", [*PYTEST, "-m", "not login"])


def login():
    command("tests about logins and sessions", [*PYTEST, "-m", "login"])


def install():
    with tempfile.TemporaryDirectory(prefix="streamrip-install-") as tmp:
        # install.sh makes a .venv of its own in its folder: not in your checkout.
        copy = Path(tmp) / "streamrip"
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\0")
        for name in filter(None, tracked):
            if (ROOT / name).is_file():
                (copy / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, copy / name)
        windows = os.name == "nt"
        script = ["pwsh", "install.ps1"] if windows else ["./install.sh"]
        if command("installer", script, cwd=copy):
            binary = copy / (
                ".venv/Scripts/streamrip.exe" if windows else ".venv/bin/streamrip"
            )
            command("installed streamrip runs", [binary, "--help"], cwd=copy)


def container():
    if not (engine := shutil.which("podman") or shutil.which("docker")):
        record("container", skipped="neither podman nor docker is installed")
    # --file: docker only looks for a Dockerfile by itself.
    elif command(
        "container build",
        [engine, "build", "--file", "Containerfile", "--tag", "streamrip:check", "."],
    ):
        command(
            "container runs", [engine, "run", "--rm", "streamrip:check", "--version"]
        )


def nix():
    if shutil.which("nix"):
        features = ["--extra-experimental-features", "nix-command flakes"]
        command(
            "nix flake check",
            ["nix", *features, "flake", "check", "--print-build-logs"],
        )
    else:
        record("nix flake check", skipped="nix is not installed")


def live(source: str, config: Path | None, query_override: str | None):
    name = f"{source}, real account"
    # Without a terminal (CI) nobody can log in: only the logins in --config
    # are used, and a source without a working one is skipped, not failed.
    unattended = not sys.stdin.isatty()
    if unattended and config is None:
        record(name, skipped="it needs a terminal to log in, or --config")
        return
    if not unattended and (
        input(f"\n{name}: streamrip will ask you to log in. Start? [Y/n] ")
        .lower()
        .startswith("n")
    ):
        record(name, skipped="you said no")
        return

    with tempfile.TemporaryDirectory(prefix=f"streamrip-live-{source}-") as tmp:
        home, music = Path(tmp) / "home", Path(tmp) / "music"
        env = {
            **os.environ,
            **dict.fromkeys(
                ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA"), str(home)
            ),
        }
        program = "from streamrip.rip import rip; raise SystemExit(rip())"

        def streamrip(*args, **kwargs):
            return subprocess.run(
                [sys.executable, "-c", program, "--folder", str(music), *args],
                env=env,
                text=True,
                **kwargs,
                check=False,
            )

        config_path = Path(
            streamrip("config", "path", capture_output=True).stdout.strip()
        )
        if config:
            import tomlkit  # not at the top: the script starts outside its environment

            doc = tomlkit.parse(Path(config).read_text())
            # Scratch databases as well, or the test download would be recorded
            # in yours (and skipped there when you have it already).
            doc["database"]["downloads_path"] = str(Path(tmp) / "downloads.db")
            doc["database"]["failed_downloads_path"] = str(Path(tmp) / "failed.db")
            config_path.write_text(tomlkit.dumps(doc))

        if unattended:
            checked = subprocess.run(
                [sys.executable, "-c", LOGIN_CHECK, str(config_path), source],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if checked.returncode in {2, 3}:
                record(name, skipped=checked.stdout.strip().splitlines()[-1])
                return
            if checked.returncode:
                record(f"{source}: log in", checked.stderr.strip()[-300:])
                return

        def downloaded():
            return sorted(p for p in music.rglob("*") if p.suffix.lower() in AUDIO)

        query = query_override or QUERIES.get(source, QUERY)
        results_file = Path(tmp) / "results.json"
        # The first command asks for the login.
        code = streamrip(
            "search", source, "track", query, "-n", "20", "-o", str(results_file)
        ).returncode
        results = json.loads(results_file.read_text()) if results_file.exists() else []
        record(
            f"{source}: log in and search",
            None
            if code == 0 and results
            else f"exit code {code}, {len(results)} results",
        )
        # Not every result can be downloaded (SoundCloud marks some _non_streamable, and
        # answers others with a 401), so the first few are tried.
        candidates = [r["id"] for r in results if "_non_streamable" not in r["id"]][:5]
        for item in candidates:
            download = ["id", source, "track", item]
            code = streamrip(*download).returncode
            if code == 0 and len(files := downloaded()) == 1:
                break
        else:
            record(
                f"{source}: download a track",
                f"none of {len(candidates)} results could be downloaded; try another --query",
            )
            return
        record(f"{source}: download a track")

        import mutagen  # not at the top: the script starts outside its environment

        problems = []
        audio = mutagen.File(files[0], easy=True)
        if audio is None:
            problems.append("not an audio file")
        else:
            problems += [
                f"no {tag} tag"
                for tag in ("artist", "title", "album")
                if not audio.get(tag)
            ]
            if audio.info.length < 30:
                problems.append(f"only {audio.info.length:.0f} seconds long")
        if ffmpeg := shutil.which("ffmpeg"):
            decoded = subprocess.run(
                [ffmpeg, "-v", "error", "-i", str(files[0]), "-f", "null", "-"],
                capture_output=True,
                text=True,
                check=False,
            )
            if decoded.returncode or decoded.stderr.strip():
                problems.append(
                    f"does not decode cleanly: {decoded.stderr.strip()[:200]}"
                )
        record(f"{source}: the track is whole and tagged", "; ".join(problems))

        # By the file, not by what is said: the wording changes.
        written = files[0].stat().st_mtime_ns
        again = streamrip(*download, capture_output=True)
        record(
            f"{source}: the same track again is skipped",
            None
            if again.returncode == 0
            and downloaded() == files
            and files[0].stat().st_mtime_ns == written
            else "it was downloaded again, or the run failed",
        )

        if (
            source == "tidal"
        ):  # an expired access token is refreshed, not a reason to log in again
            text = config_path.read_text(encoding="utf-8")
            section = re.search(r"(?ms)^\[tidal\]\n.*?(?=^\[|\Z)", text)
            expired = re.sub(
                r"(?m)^((?:hires_)?token_expiry) = .*$", r"\1 = 0", section[0]
            )
            config_path.write_text(text.replace(section[0], expired), encoding="utf-8")
            out = streamrip(
                "search",
                "tidal",
                "track",
                QUERY,
                "-o",
                str(Path(tmp) / "results.txt"),
                capture_output=True,
            )
            expiry = tomllib.loads(config_path.read_text(encoding="utf-8"))["tidal"][
                "token_expiry"
            ]
            record(
                "tidal: an expired login is renewed",
                None
                if out.returncode == 0 and float(expiry or 0) > time.time()
                else f"exit code {out.returncode}, token_expiry {expiry!r}",
            )


def _flag_value(argv: list[str], flag: str) -> str | None:
    """The value after flag, removed from argv along with the flag.

    A flag with nothing after it is a typo: show how to run this, like any
    other bad invocation, instead of crashing on argv[i + 1].
    """
    if flag not in argv:
        return None
    i = argv.index(flag)
    if i + 1 == len(argv):
        sys.exit(__doc__)
    value = argv[i + 1]
    del argv[i : i + 2]
    return value


def main(argv: list[str]):
    config_arg = _flag_value(argv, "--config")
    config = Path(config_arg).expanduser() if config_arg else None
    query = _flag_value(argv, "--query")
    what, extra = (argv[0], argv[1:]) if argv else ("", [])
    steps = {
        "offline": offline,
        "login": login,
        "install": install,
        "container": container,
        "nix": nix,
    }
    if (
        what not in {*steps, "live", "all"}
        or (extra and what != "live")
        or set(extra) - set(SOURCES)
    ):
        sys.exit(__doc__)

    try:
        for step in (
            steps.values() if what == "all" else [steps[what]] if what in steps else []
        ):
            step()
        if what in {"live", "all"}:
            for source in extra or SOURCES:
                try:
                    live(source, config, query)
                except Exception as e:  # one service's crash must not skip the next
                    record(f"{source}, real account", f"{type(e).__name__}: {e}")
    except KeyboardInterrupt:
        print("\nStopped.")
    print("\n== Summary")
    for name, outcome in report:
        print(f"  {name:48} {outcome}")
    sys.exit(any(outcome.startswith("FAILED") for _, outcome in report))


if __name__ == "__main__":
    # Started by any python: run again in the project's environment, where
    # streamrip and mutagen are installed.
    if "STREAMRIP_CHECKS" not in os.environ:
        os.environ["STREAMRIP_CHECKS"] = "1"
        os.execvp(
            UV[0], [*UV, "--project", str(ROOT), "python", __file__, *sys.argv[1:]]
        )
    main(sys.argv[1:])
