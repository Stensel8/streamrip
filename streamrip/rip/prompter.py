import asyncio
import contextlib
import logging
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
lazy from collections.abc import Callable

import click

from ..client.spotify import DEVELOPER_DASHBOARD
from ..console import console
from ..exceptions import AuthenticationError, MissingCredentialsError
from .browser_login import browser_login_available, has_screen
from .deezer_arl_capture import DeezerArlCaptureError, capture_deezer_arl_via_browser
from .interactive import Confirm, Prompt
from .qobuz_token_capture import (
    QobuzTokenCaptureError,
    capture_qobuz_auth_token,
    capture_qobuz_auth_token_via_browser,
    oauth_redirect,
    qobuz_oauth_code,
)
from .spotify_login import (
    SpotifyLoginError,
    check_redirect,
    redirect_params,
    wait_for_redirect,
)
lazy from ..client import (
    Client,
    DeezerClient,
    QobuzClient,
    SoundcloudClient,
    SpotifyClient,
    TidalClient,
)
lazy from ..client.soloist import SoloistBackend
lazy from ..config import Config

logger = logging.getLogger("streamrip")


def launch(url: str) -> None:
    """click.launch, but keeping the browser off the terminal: one already running
    prints over the prompt there ("Opening in existing browser session.")."""
    if sys.platform.startswith("linux") and shutil.which("xdg-open"):
        subprocess.Popen(
            ["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    else:
        click.launch(url)


def _open_login_link(url: str) -> None:
    """Ask the system default browser to open a link already printed to the user."""
    # No browser available (e.g. over SSH): the printed link is the way there.
    with contextlib.suppress(Exception):
        launch(url)


def _show_login_link(url: str) -> None:
    """Print the login page's address, then open it.

    Qobuz and Deezer print no link of their own, and there may be no browser to
    open it (a container, SSH): then the printed address is the way to the page.
    """
    console.print(f"Log in at [blue underline]{url}[/blue underline]")
    _open_login_link(url)


def _choose_login(service: str, options: list[tuple[str, str]], default: str) -> str:
    """Show the numbered ways to log in, and return the key of the one chosen."""
    if len(options) == 1:
        return options[0][0]  # nothing to choose between
    listing = "".join(f"  {n}. {text}\n" for n, (_, text) in enumerate(options, 1))
    console.print(f"\nHow do you want to log in to {service}?\n{listing}")
    keys = [key for key, _ in options]
    choice = Prompt.ask(
        "Choose",
        choices=[str(n) for n in range(1, len(options) + 1)],
        default=str(keys.index(default) + 1),
    )
    return keys[int(choice) - 1]


async def browser_code(
    url: str,
    redirect_uri: str,
    state: str,
    label: str,
    read: Callable[[dict[str, str]], str] | None = None,
) -> str:
    """Send the user to `url` to log in, and take the code back: caught on
    this computer, or pasted from a browser elsewhere. `read` takes the code
    from the redirect's query; by default an OAuth code of the user's own app,
    whose `state` it checks."""
    own_app = read is None
    if read is None:
        read = lambda params: check_redirect(params, state, label)  # noqa: E731
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
        console.print(f"\nOpening your browser. If nothing opens, go to:\n{url}\n")
        if own_app:
            console.print(
                f"{label} must send you back to [bold]{redirect_uri}[/bold]; add "
                "that to your app as a redirect URI if it says it is not allowed."
            )
        _open_login_link(url)
        return read(await wait_for_redirect(redirect_uri))
    console.print(f"\nOpen this link and log in:\n{url}\n")
    console.print(
        "The page you end up on will not load: that is as it should be. "
        "Copy its whole address from the address bar."
    )
    return read(redirect_params(Prompt.ask("Paste the address")))


class CredentialPrompter(ABC):
    client: Client

    def __init__(self, config: Config, client: Client):
        """Prompt for `client`'s source and save into `config`."""
        self.config = config
        self.client = client

    @abstractmethod
    def has_creds(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def prompt_and_login(self):
        """Prompt for credentials in the appropriate way,
        and save them to the configuration.
        """
        raise NotImplementedError

    @abstractmethod
    def save(self):
        """Save current config to file"""
        raise NotImplementedError

    def _announce_saved(self):
        """Say where the credentials just went."""
        console.print(
            f"[green]Login saved to [bold cyan]{self.config.state.path or self.config.path}",
        )


class QobuzPrompter(CredentialPrompter):
    client: QobuzClient

    def has_creds(self) -> bool:
        c = self.config.session.qobuz
        return c.user_id != "" and c.auth_token != ""

    async def prompt_and_login(self):
        if not self.has_creds():
            await self._prompt_creds_and_set_session_config()

        while True:
            try:
                await self.client.login()
                break
            except AuthenticationError as e:
                console.print(str(e), style="yellow", markup=False)
                await self._prompt_creds_and_set_session_config()
            except MissingCredentialsError:
                await self._prompt_creds_and_set_session_config()

    async def _oauth(self) -> tuple[str, str]:
        """Qobuz's own login page, as its web player logs in."""
        redirect = oauth_redirect()
        url = await self.client.oauth_link(redirect)
        code = await browser_code(url, redirect, "", "Qobuz", qobuz_oauth_code)
        return await self.client.oauth_token(code)

    async def _prompt_creds_and_set_session_config(self):
        """Ask for a user id + user_auth_token: from Qobuz's own login page
        first, else from a logged-in browser (see qobuz_token_capture), else
        by hand.
        """
        options = [
            (
                "oauth",
                (
                    "Log in on Qobuz's own page, here or on another device:\n"
                    "     streamrip gets a login of its own"
                ),
            )
        ]
        if browser_login_available():
            options.append(
                (
                    "window",
                    (
                        "Open an isolated browser window that logs in and captures\n"
                        "     the token automatically"
                    ),
                )
            )
        # The script posts the token to 127.0.0.1, which only a browser on this
        # machine can reach.
        screen = has_screen()
        if screen:
            options.append(
                (
                    "script",
                    (
                        "Log in in your own browser, then paste a short script into\n"
                        "     its console to send the token back"
                    ),
                )
            )
        options.append(("hand", "Copy the user id and token from your browser by hand"))
        choice = _choose_login("Qobuz", options, default="oauth")

        if choice == "oauth":
            try:
                user_id, token = await self._oauth()
            except (SpotifyLoginError, AuthenticationError) as e:
                console.print(str(e), style="yellow", markup=False)
            else:
                self._set_session_creds(user_id, token)
                return
        elif choice == "window":
            try:
                user_id, token = await capture_qobuz_auth_token_via_browser()
            except QobuzTokenCaptureError as e:
                console.print(str(e), style="yellow", markup=False)
            else:
                self._set_session_creds(user_id, token)
                return
        elif choice == "script":
            _show_login_link("https://play.qobuz.com/login")
            try:
                user_id, token = await capture_qobuz_auth_token()
            except QobuzTokenCaptureError as e:
                console.print(str(e), style="yellow", markup=False)
            else:
                self._set_session_creds(user_id, token)
                return

        _show_login_link("https://play.qobuz.com/login")
        console.print(
            "\nEnter it manually instead:\n"
            "  1. In the browser tab that just opened, open DevTools -> Network,\n"
            "     then log in (log out first if needed)\n"
            "  2. Find the [bold]user/login[/bold] request and open its response\n"
            "  3. Copy [bold]user.id[/bold] and [bold]user_auth_token[/bold]\n"
        )
        user_id = ""
        while not user_id:
            user_id = Prompt.ask("Enter your Qobuz user id").strip()
        token = ""
        while not token:
            token = Prompt.ask(
                "Enter your Qobuz user_auth_token (invisible)", password=True
            ).strip()
        self._set_session_creds(user_id, token)

    def _set_session_creds(self, user_id: str, token: str):
        """Keep the user id and token in the session's config."""
        c = self.config.session.qobuz
        c.user_id = user_id
        c.auth_token = token

    def save(self):
        """Write the session's Qobuz credentials to the config file."""
        c = self.config.session.qobuz
        cf = self.config.file.qobuz
        cf.user_id = c.user_id
        cf.auth_token = c.auth_token
        self.config.file.set_modified()
        self._announce_saved()


class TidalPrompter(CredentialPrompter):
    client: TidalClient
    # Where the login link goes, with the minutes it lasts and which login it
    # is: None prints it and opens a browser; the web interface shows it.
    show_link: Callable[[str, int, str], None] | None = None

    def has_creds(self) -> bool:
        return all(lane.tokens.access_token for lane in self.client.lanes())

    async def prompt_and_login(self):
        """Log in every lane that has no working login yet."""
        for lane in self.client.lanes():
            if lane.tokens.access_token:
                try:
                    await lane._login_lane()
                    continue
                except AuthenticationError, MissingCredentialsError:
                    pass
            await self._device_login(lane)
        self.client.logged_in = True

    async def _device_login(self, lane: TidalClient):
        """Walk the user through Tidal's device login flow for one lane."""
        which = "CD quality" if lane is self.client else "hi-res"
        if len(self.client.lanes()) > 1 and self.show_link is None:
            console.print(
                "Tidal serves hi-res and CD quality through two separate logins. "
                f"This one is for {which}."
            )
        device_code, uri, expires_in = await lane._get_device_code()
        login_link = uri if uri.startswith("http") else f"https://{uri}"

        # Tidal says how long the link stays valid (5 minutes, lately).
        if self.show_link is not None:
            self.show_link(login_link, expires_in // 60, which)
        else:
            console.print(
                f"Go to [blue underline]{login_link}[/blue underline] to log into "
                f"Tidal within {expires_in // 60} minutes.",
            )
            _open_login_link(login_link)

        # A few seconds early: Tidal's clock started before its answer arrived.
        deadline = time.monotonic() + expires_in - 5
        while True:
            if time.monotonic() > deadline:
                raise AuthenticationError(
                    "The Tidal login link expired before it was used. Run "
                    "streamrip again for a new one."
                )
            status, info = await lane._get_auth_status(device_code)
            if status == 0:
                break
            if status == 1:
                raise AuthenticationError(
                    f"Tidal rejected the device login ({info['error']}). Try "
                    "again, or check the [tidal] client settings in the config."
                )
            await asyncio.sleep(4)  # still waiting for the login

        c = self.config.session.tidal
        c.user_id = info["user_id"]  # type: ignore
        c.country_code = info["country_code"]  # type: ignore
        t = lane.tokens
        t.access_token = info["access_token"]  # type: ignore
        t.refresh_token = info["refresh_token"]  # type: ignore
        t.token_expiry = info["token_expiry"]  # type: ignore
        t.token_client_id = lane.client_id

        lane._update_authorization_from_config()
        lane.logged_in = True

    def save(self):
        c = self.config.session.tidal
        cf = self.config.file.tidal
        cf.user_id = c.user_id
        cf.country_code = c.country_code
        for lane in self.client.lanes():
            lane.save_login()
        self.config.file.set_modified()


class DeezerPrompter(CredentialPrompter):
    client: DeezerClient

    def has_creds(self):
        c = self.config.session.deezer
        return c.arl != ""

    async def prompt_and_login(self):
        """Ask for an ARL unless one is saved, and again until Deezer accepts it."""
        if not self.has_creds():
            await self._prompt_creds_and_set_session_config()
        while True:
            try:
                await self.client.login()
                break
            except AuthenticationError:
                console.print("[yellow]Invalid arl, try again.")
                await self._prompt_creds_and_set_session_config()

    async def _prompt_creds_and_set_session_config(self):
        """Ask for an ARL cookie.

        Deezer has no sign-in for outside apps, so the ARL of a logged-in web
        session is the only login there is; offer to capture it from an
        isolated browser window (see deezer_arl_capture) before asking for it
        outright.
        """
        options = []
        if browser_login_available():
            options.append(
                (
                    "window",
                    (
                        "Open an isolated browser window that logs in and captures\n"
                        "     the ARL cookie automatically"
                    ),
                )
            )
        options.append(("hand", "Copy the ARL cookie from your browser by hand"))
        choice = _choose_login("Deezer", options, default="hand")

        if choice == "window":
            try:
                arl = await capture_deezer_arl_via_browser()
            except DeezerArlCaptureError as e:
                console.print(str(e), style="yellow", markup=False)
            else:
                self.config.session.deezer.arl = arl
                return

        _show_login_link("https://www.deezer.com/login")
        console.print(
            "\nEnter it manually instead:\n"
            "  1. In the browser tab that just opened, log in to Deezer\n"
            "  2. Open DevTools (F12) -> Application (Chrome, Edge, Brave) or\n"
            "     Storage (Firefox) -> Cookies -> https://www.deezer.com\n"
            "  3. Copy the [bold]Value[/bold] of the [bold]arl[/bold] cookie\n"
        )
        arl = ""
        while not arl:
            arl = Prompt.ask("Enter your Deezer ARL (invisible)", password=True).strip()
        self.config.session.deezer.arl = arl

    def save(self):
        """Write the session's ARL to the config file."""
        c = self.config.session.deezer
        cf = self.config.file.deezer
        cf.arl = c.arl
        self.config.file.set_modified()
        self._announce_saved()


class SoundcloudPrompter(CredentialPrompter):
    """SoundCloud works as a guest, so a download never stops to ask. A login
    (its oauth_token cookie) gives better quality and higher limits, and is
    asked for by `streamrip login soundcloud`."""

    client: SoundcloudClient

    def has_creds(self) -> bool:
        return True

    async def prompt_and_login(self):
        """Ask for the oauth_token, and keep it if SoundCloud takes it. Empty:
        stay a guest."""
        console.print(
            "\nA SoundCloud login gives better quality and higher limits. Log in "
            "at [blue underline]https://soundcloud.com/signin[/blue underline], open "
            "DevTools → Application (or Storage) → Cookies and copy the value of "
            "the [bold]oauth_token[/bold] cookie. Leave it empty to stay a guest.\n"
        )
        token = Prompt.ask("oauth_token", password=True).strip()
        if not token:
            return
        self.config.session.soundcloud.oauth_token = token
        await self.client.login()
        if self.client.guest_reason:
            self.config.session.soundcloud.oauth_token = ""
            raise AuthenticationError(self.client.guest_reason)

    def save(self):
        self.config.file.soundcloud.oauth_token = (
            self.config.session.soundcloud.oauth_token
        )
        self.config.file.set_modified()


class SpotifyPrompter(CredentialPrompter):
    """Spotify: the user's own app, for the browser login."""

    client: SpotifyClient

    def has_creds(self) -> bool:
        c = self.config.session.spotify
        return bool(c.refresh_token or c.access_token)

    async def prompt_and_login(self):
        await self._log_in_app()
        # Kept now: the pairing after it waits for the Spotify app, and a
        # Ctrl-C there must not cost the login just made.
        self._keep()
        await self._offer_spotify_backend()

    async def _log_in_app(self):
        """The app of one's own: ask for it unless it is saved, then log in."""
        c = self.config.session.spotify
        if not c.client_id:
            self._explain_app()
            while not c.client_id:
                c.client_id = Prompt.ask(
                    "Enter the Client ID of your Spotify app"
                ).strip()
        while True:
            try:
                await self._log_in()
            except (SpotifyLoginError, AuthenticationError) as e:
                console.print(str(e), style="yellow", markup=False)
                if not Confirm.ask("Try again?", default=True):
                    raise AuthenticationError("Spotify login cancelled.") from e
            else:
                break

    async def _offer_spotify_backend(self):
        """After the Web-API login: sign in to Spotify's own audio too.

        A bonus on top of the login, so it never fails or blocks it: no
        terminal, or a declined question, or a failed sign-in, just leaves
        the login as it is.
        """
        c = self.config.session.spotify
        if c.backend != "spotify":
            return
        # Never block on a prompt nobody is there to answer: streamrip is run
        # from cron and from scripts, where a hidden y/n means hanging forever.
        if not sys.stdin.isatty():
            return
        if not SoloistBackend(c).supported:
            # Not asked for a key that cannot be used here.
            console.print(
                "[yellow]Spotify's own audio needs Linux with PipeWire or "
                "PulseAudio, which this computer does not have: the audio comes "
                "from YouTube Music.[/yellow]"
            )
            return
        if not Confirm.ask(
            "Also set up Spotify's own audio, at the account's quality? It "
            "goes through Spotify's official client, Soloist.",
            default=True,
        ):
            return
        key = c.soloist_api_key
        if not key:
            console.print(
                "\nSpotify's own audio needs a [bold]Soloist API key[/bold]: one "
                "per person, free for Premium accounts, made at\n"
                "[blue underline]https://developer.spotify.com/dashboard/soloist"
                "[/blue underline]\n"
                "It is a secret, like a password: streamrip never prints it, "
                "logs it or sends it anywhere but Spotify."
            )
            while not key:
                key = Prompt.ask("Enter the Soloist API key", password=True).strip()
            c.soloist_api_key = key
            self._keep()  # the key, whatever happens to the pairing
        backend = self.client.soloist_backend
        if backend is None:
            console.print(
                "[yellow]The [spotify] backend is on, but no Soloist API key "
                "is saved; the audio will come from YouTube Music.[/yellow]"
            )
            return
        # Paired before: pairing again waits for the Spotify app, so only when
        # asked for.
        if backend.paired() and not Confirm.ask(
            "This computer is paired with Spotify already. Pair it again?",
            default=False,
        ):
            console.print("Spotify's own audio is set up.")
            return
        try:
            with console.status("Getting Spotify's official client ready"):
                await asyncio.to_thread(backend.ensure_binary)  # downloaded once
            console.print(
                "\nPair this computer, in the Spotify app on a device on this network:\n"
                "  1. Open the app and press play\n"
                "  2. Tap [italic]Connect to a device[/italic]\n"
                f"  3. Pick [bold]{backend.device_name}[/bold]\n"
                "streamrip waits for that, up to 10 minutes (Ctrl-C stops; your login "
                "is kept)."
            )
            username = await self.client.pair_soloist(key)
        except Exception as e:  # a failed download or pairing: the login stands
            console.print(str(e), style="yellow", markup=False)
            return
        console.print(f"Signed in to Spotify's own audio as {username}.")

    def _explain_app(self):
        """Say what the user has to make, and where, before streamrip can log in."""
        redirect_uri = self.config.session.spotify.redirect_uri
        console.print(
            "\n[bold]Spotify needs an app of your own to log in with.[/bold] It "
            "takes a minute, and is free,\nbut [bold]the account that owns the "
            "app must have Spotify Premium[/bold]: since February 2026 Spotify "
            "blocks the API for apps of free accounts. If you have no Premium, "
            "someone who does can make the app and add your Spotify account "
            "under [italic]Settings > User Management[/italic].\n\n"
            f"  1. Go to [blue underline]{DEVELOPER_DASHBOARD}[/blue underline] "
            "and log in, then choose [italic]Create app[/italic].\n"
            "  2. Any name and description will do.\n"
            f"  3. Redirect URI: [bold]{redirect_uri}[/bold]\n"
            "     (exactly that, with 127.0.0.1 and not localhost)\n"
            "  4. Under [italic]Which API/SDKs are you planning to use?[/italic] "
            "tick [bold]Web API[/bold] only, accept the terms and save.\n"
            "  5. Open the app's [italic]Settings[/italic] and copy its "
            "[bold]Client ID[/bold]. You do not need the client secret.\n\n"
            "streamrip uses Spotify for the track lists, tags and covers. The audio "
            "is Spotify's own once Soloist is set up, and a YouTube Music match "
            "until then.\n"
        )
        _open_login_link(DEVELOPER_DASHBOARD)

    async def _log_in(self):
        """One browser login: send the user to Spotify and take the code back."""
        url, state, verifier = self.client.authorization_url()
        redirect_uri = self.config.session.spotify.redirect_uri
        code = await browser_code(url, redirect_uri, state, "Spotify")
        await self.client.finish_login(code, verifier)

    def save(self):
        """Write the session's Spotify app and login to the config file."""
        self._keep()
        self._announce_saved()

    def _keep(self):
        """Write the login as it is now, before a step that can be stopped."""
        c = self.config.session.spotify
        cf = self.config.file.spotify
        for name in (
            "client_id",
            "access_token",
            "refresh_token",
            "token_expiry",
            "scope",
            "backend",
            "device_name",
            "soloist_api_key",
        ):
            setattr(cf, name, getattr(c, name))
        self.config.file.set_modified()
        self.config.save_file()


PROMPTERS = frozendict(
    {
        "qobuz": QobuzPrompter,
        "deezer": DeezerPrompter,
        "tidal": TidalPrompter,
        "soundcloud": SoundcloudPrompter,
        "spotify": SpotifyPrompter,
    }
)


def get_prompter(client: Client, config: Config) -> CredentialPrompter:
    """Return an instance of a prompter."""
    p = PROMPTERS[client.source]
    return p(config, client)
