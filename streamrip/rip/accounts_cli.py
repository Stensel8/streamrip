"""`streamrip accounts`, `login` and `logout`: the logins of every service.

What is logged in is in streamrip/accounts.py; the logins themselves are kept in
state.toml, next to the config.
"""

import secrets
lazy import asyncio

import click

from ..accounts import BY_ID, LOGIN_IDS, Accounts
lazy from ..client import CLIENTS
lazy from ..config import Config
lazy from ..console import console
lazy from ..sync.auth import pkce_pair
lazy from ..sync.errors import SyncError
lazy from ..sync.services import DEVELOPER_DASHBOARDS, Services
lazy from ..verify import FAILED
lazy from ..verify import verify as run_verify
lazy from .cli import _require_config, coro
lazy from .interactive import Prompt
lazy from .prompter import get_prompter, has_screen, launch
lazy from .spotify_login import capture_code, code_from_redirect_url

LOGIN = click.Choice(LOGIN_IDS, case_sensitive=False)


@click.command()
@click.pass_context
def accounts(ctx):
    """Show which services you are logged in to."""
    config = _require_config(ctx)
    services = Services(config)
    for status in Accounts(config).all():
        login = status.login
        state = (
            "logged in"
            if status.logged_in
            else "needs an app first"
            if status.app_missing
            else "guest"
            if login.guest
            else "not logged in"
        )
        console.print(
            f"{login.id:14} {state:19} {login.uses}", highlight=False, soft_wrap=True
        )
        if login.app:
            console.print(
                f"{'':14} redirect URI: {services.redirect_uri(login.section)}",
                highlight=False,
            )
    console.print(f"Kept in {config.state.path}", highlight=False, soft_wrap=True)


def _log_in_again(config: Config, service: str):
    """Log in to `service` from nothing, and put the old login back if that does
    not work out: a login that works is not asked for twice."""
    accounts = Accounts(config)
    held = accounts.logout(service)
    try:
        if service == "tidal-library":
            _log_in_to_tidal_library(Services(config))
        else:
            coro(_log_in)(config, service)
    except SyncError as e:
        accounts.restore(service, held)
        raise click.ClickException(str(e)) from e
    except KeyboardInterrupt:
        accounts.restore(service, held)
        raise click.ClickException("Login cancelled.") from None
    except BaseException:
        accounts.restore(service, held)
        raise
    # `coro` ends a Ctrl-C quietly, so the end of the call says nothing yet:
    # what was saved does.
    if Accounts(Config(config.path)).logged_in(service):
        console.print(f"Logged in to {BY_ID[service].label}.")
    else:
        accounts.restore(service, held)
        console.print(f"No login made: {BY_ID[service].label} stays as it was.")


@click.command()
@click.argument("service", type=LOGIN, required=False)
@click.pass_context
def login(ctx, service):
    """Log in to a service, or log in again.

    tidal logs in for downloading. tidal-library is Tidal's official API, for
    your playlists and favourites, with a developer app of your own. spotify
    is one login for downloading and playlists alike, with an app of your own
    too. Without a name, it asks.
    """
    config = _require_config(ctx)
    service = service or Prompt.ask(
        "Log in to",
        choices=list(LOGIN_IDS),
        default=next(
            (s.login.id for s in Accounts(config).all() if not s.logged_in), None
        ),
    )
    _log_in_again(config, service)


@click.command()
@click.argument("service", type=LOGIN, required=False)
@click.option(
    "--fix",
    is_flag=True,
    help="Log in to what is not logged in or no longer works, one after the other.",
)
@click.pass_context
def verify(ctx, service, fix):
    """Try every login against its service, and say what to do about it.

    `streamrip accounts` shows what is saved; this asks the services, and tells
    you the account's plan, whether your quality setting fits it, and which
    command fixes what does not work. Exits 1 when a saved login fails.
    """
    config = _require_config(ctx)
    ids = (service,) if service else LOGIN_IDS
    results = coro(run_verify)(config, ids)
    _print_results(results)
    if fix:
        for result in (r for r in results if r.fixable):
            console.print(f"\n[bold]{BY_ID[result.id].label}[/bold]")
            try:
                _log_in_again(config, result.id)
            except click.ClickException as e:
                console.print(f"[red]{e.message}[/red]")
            except SystemExit:  # `coro` exits for a login that was refused
                console.print(
                    f"[red]{BY_ID[result.id].label}: the login did not work.[/red]"
                )
        results = coro(run_verify)(config, ids)
        console.print()
        _print_results(results)
    elif any(r.fixable for r in results):
        console.print("\nRun `streamrip verify --fix` to log in to these.")
    if any(r.state == FAILED for r in results):
        ctx.exit(1)


def _print_results(results):
    for r in results:
        arrow = f"→ {r.fix}" if r.fix else ""
        what = " · ".join(filter(None, (r.detail, r.plan, r.formats)))
        console.print(
            f"{r.id:14} {r.state:14} {what} {arrow}".rstrip(),
            highlight=False,
            soft_wrap=True,
            markup=False,
        )
        if r.hint:
            console.print(
                f"{'':14} {r.hint}", highlight=False, soft_wrap=True, markup=False
            )


@click.command()
@click.argument("service", type=LOGIN)
@click.pass_context
def logout(ctx, service):
    """Forget the login of a service.

    The app you made for it stays, so logging in again is one step. Spotify's
    is the login for downloading and playlists alike.
    """
    label = BY_ID[service].label
    forgotten = Accounts(_require_config(ctx)).logout(service)
    console.print(
        f"Logged out of {label}." if forgotten else f"Not logged in to {label}."
    )


async def _log_in(config: Config, source: str):
    """Log in to a download source as it does the first time it is used."""
    client = CLIENTS[source](config)
    try:
        prompter = get_prompter(client, config)
        await prompter.prompt_and_login()
        prompter.save()
    finally:
        await client.close()
    config.save_file()


# --- Tidal's official API ----------------------------------------------------


def _browser_code(url: str, redirect_uri: str, state: str, label: str) -> str:
    """Send the user to `url` to log in, and take the code back: caught on
    this computer, or pasted from a browser elsewhere."""
    console.print(
        f"\nHow do you want to log in to {label}?\n"
        "  1. In the browser on this computer: streamrip catches the login itself\n"
        "  2. In a browser on another device (streamrip runs on a server, say):\n"
        "     open the link there and paste the address you end up on\n"
    )
    choice = Prompt.ask(
        "Choose", choices=["1", "2"], default="1" if has_screen() else "2"
    )
    if choice == "1":
        console.print(
            f"\nOpening your browser. If nothing opens, go to:\n{url}\n"
            f"{label} must send you back to [bold]{redirect_uri}[/bold]; add that "
            "to your app as a redirect URI if it says it is not allowed."
        )
        launch(url)
        return asyncio.run(capture_code(redirect_uri, state, label))
    console.print(f"\nOpen this link and log in:\n{url}\n")
    console.print(
        "The page you end up on will not load: that is as it should be. "
        "Copy its whole address from the address bar."
    )
    return code_from_redirect_url(Prompt.ask("Paste the address"), state, label)


def _tidal_app(services: Services):
    """Ask for the Tidal developer app, unless there is one."""
    tidal = services.config.session.tidal
    if tidal.api_client_id and tidal.api_client_secret:
        return
    console.print(
        "\n[bold]Tidal's official API needs a developer app of your own.[/bold] "
        "It is free and takes a minute. (Downloading from Tidal does not need it; "
        "only reading and changing your playlists and favourites does.)\n\n"
        f"  1. Go to [blue underline]{DEVELOPER_DASHBOARDS['tidal']}[/blue underline] "
        "and choose [italic]Create New App[/italic].\n"
        f"  2. Redirect URI: [bold]{tidal.api_redirect_uri}[/bold]\n"
        "  3. Scopes: collection.read, collection.write, playlists.read, "
        "playlists.write and user.read\n"
        "  4. Copy its Client ID and Client Secret.\n"
    )
    launch(DEVELOPER_DASHBOARDS["tidal"])
    client_id = ""
    while not client_id:
        client_id = Prompt.ask("Enter the Client ID of your Tidal app").strip()
    secret = ""
    while not secret:
        secret = Prompt.ask(
            "Enter its Client Secret (invisible)", password=True
        ).strip()
    services.config.update_logins(
        "tidal", {"api_client_id": client_id, "api_client_secret": secret}
    )


def _log_in_to_tidal_library(services: Services):
    """Log in to Tidal's official API in the browser, and keep the login."""
    _tidal_app(services)
    oauth = services.oauth("tidal")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    code = _browser_code(
        oauth.authorize_url(state, challenge), oauth.redirect_uri, state, "Tidal"
    )
    services.store.save("tidal", oauth.exchange(code, verifier))
