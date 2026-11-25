# Checks by hand

CI runs everything that needs no account. The scripts here run the rest on your
own machine, and `run.py all` runs everything, so you don't need CI to test
streamrip completely.

```bash
python checks/run.py offline     # CI's lint and the tests that need no login
python checks/run.py login       # the tests about logins, tokens and sessions
python checks/run.py live        # real accounts; or: live tidal qobuz
python checks/run.py install     # CI's installer job, on a copy of this checkout
python checks/run.py container   # CI's image build and smoke test (podman or docker)
python checks/run.py nix         # CI's `nix flake check`
python checks/run.py all
```

Any Python and any folder will do. The script reruns itself in the project's
environment with uv.

| | CI | by hand |
| --- | --- | --- |
| lint, format, tests without logins | yes | `offline` |
| installer (Linux, macOS, Windows) | yes | `install`, on your system |
| image build and smoke test | yes | `container` |
| `nix flake check` | yes | `nix` (it only sees files git tracks) |
| tests marked `login`: prompters, token capture, device login, refresh (mocks and local pages) | no | `login` |
| real accounts | on the pi, with the `STREAMRIP_STATE` secret | `live` |

## Real accounts

`live` asks before each service, and then streamrip asks you to log in (a link
for Tidal, a browser for Qobuz, Deezer and Spotify). After that it:

1. logs in and searches for a track,
2. downloads one result and checks the file's tags, its length and that it
   decodes cleanly,
3. runs the same download again and checks that it's skipped,
4. for Tidal, expires the access tokens and checks that they're refreshed
   without asking for a new login.

It uses a scratch config and folder, so your own config, logins and downloads
aren't touched. Both are deleted afterwards. `--config FILE` takes the logins
from that file instead of asking for them, and `--query TEXT` searches for
something else. SoundCloud only lets streamrip download what artists upload
themselves, so its default is a generic search that can come up empty.

It does one search and one download per service. Run it when you've changed
something. Don't put it on a schedule: services suspend accounts that look like
abuse.

### In CI

On a push to `dev`, the pi runs `live` for Qobuz, Tidal and Deezer with the
`STREAMRIP_STATE` secret: the `[qobuz]`, `[tidal]` and `[deezer]` sections of
your `state.toml`, on the default config. Without the secret the job does
nothing; with it, a login that no longer works fails the job (`--strict`).
