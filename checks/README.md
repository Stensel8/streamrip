# Checks by hand

CI runs what needs no account. Everything else is run by you, with the scripts
here. You need no CI to test streamrip completely: `run.py all` does all of it.

```bash
uv run python checks/run.py offline     # CI's lint and the tests that need no login
uv run python checks/run.py login       # the tests about logins, tokens and sessions
uv run python checks/run.py live        # real accounts; or: live tidal qobuz
uv run python checks/run.py install     # CI's installer job, on a copy of this checkout
uv run python checks/run.py container   # CI's image build and smoke test (podman or docker)
uv run python checks/run.py nix         # CI's `nix flake check`
uv run python checks/run.py all
```

| | CI | by hand |
| --- | --- | --- |
| lint, format, tests without logins | yes | `offline` |
| installer (Linux, macOS, Windows) | yes | `install`, on your system |
| image build and smoke test | yes | `container` |
| `nix flake check` | yes | `nix` (it only sees files git tracks) |
| tests marked `login`: prompters, token capture, device login, refresh (mocks and local pages) | no | `login` |
| real accounts | no | `live` |

## Real accounts

`live` asks before each service, then streamrip asks you to log in (a link to
open for Tidal, a browser for Qobuz, Deezer and Spotify). It then:

1. logs in and searches for a track,
2. downloads one result and checks the file: tags, length and a clean decode,
3. runs the same download again and checks that it is skipped,
4. for Tidal, makes the access tokens expire and checks that they are refreshed
   instead of a new login being asked for.

It uses a scratch config and folder, so your own config, logins and downloads
are untouched; both are deleted afterwards. `--config FILE` starts from the
logins in that file instead of asking for them, and `--query TEXT` searches for
something else (SoundCloud only lets streamrip download what artists upload
themselves, so its default is a generic search that can come up empty).

It does one search and one download per service. Run it when you changed
something, not on a schedule: services suspend accounts that look like abuse.
