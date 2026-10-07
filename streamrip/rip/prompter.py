import asyncio
import logging
import time
from abc import ABC, abstractmethod

from click import launch

from ..client import (
    Client,
    DeezerClient,
    QobuzClient,
    SoundcloudClient,
    SpotifyClient,
    TidalClient,
)
from ..client.spotify import DEVELOPER_DASHBOARD
from ..config import Config
from ..console import console
from ..exceptions import AuthenticationError, MissingCredentialsError
from .deezer_arl_capture import DeezerArlCaptureError, capture_deezer_arl_via_browser
from .interactive import Confirm, Prompt
from .qobuz_token_capture import (
    QobuzTokenCaptureError,
    capture_qobuz_auth_token,
    capture_qobuz_auth_token_via_browser,
)
from .spotify_login import (
    SpotifyLoginError,
    capture_code,
    code_from_redirect_url,
)

logger = logging.getLogger("streamrip")


def _open_login_link(url: str) -> None:
    """Ask the system default browser to open a link already printed to the user."""
    try:
        launch(url)
    except Exception:
        # No browser available (e.g. over SSH); use the printed link manually.
        pass


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
            f"[green]Credentials saved to config file at [bold cyan]{self.config.path}",
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
                console.print(f"[yellow]{e}")
                await self._prompt_creds_and_set_session_config()
            except MissingCredentialsError:
                await self._prompt_creds_and_set_session_config()

    async def _prompt_creds_and_set_session_config(self):
        """Ask for a user id + user_auth_token.

        The token from a logged-in browser session is the only login Qobuz
        still accepts; offer the two ways to capture it automatically (see
        qobuz_token_capture) before asking for it outright.
        """
        console.print(
            "\nHow do you want to log in to Qobuz?\n"
            "  1. Open an isolated browser window that logs in and captures\n"
            "     the token automatically\n"
            "  2. Log in in your own browser, then paste a short script into\n"
            "     its console to send the token back\n"
            "  3. Copy the user id and token from your browser by hand\n"
        )
        choice = Prompt.ask("Choose", choices=["1", "2", "3"], default="2")

        if choice == "1":
            try:
                user_id, token = await capture_qobuz_auth_token_via_browser()
            except QobuzTokenCaptureError as e:
                console.print(f"[yellow]{e}")
            else:
                self._set_session_creds(user_id, token)
                return
        elif choice == "2":
            _open_login_link("https://play.qobuz.com/login")
            try:
                user_id, token = await capture_qobuz_auth_token()
            except QobuzTokenCaptureError as e:
                console.print(f"[yellow]{e}")
            else:
                self._set_session_creds(user_id, token)
                return

        _open_login_link("https://play.qobuz.com/login")
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
        self.save()

    async def _device_login(self, lane: TidalClient):
        """Walk the user through Tidal's device login flow for one lane."""
        if len(self.client.lanes()) > 1:
            console.print(
                "Tidal serves hi-res and CD quality through two separate logins. "
                f"This one is for {'CD quality' if lane is self.client else 'hi-res'}."
            )
        device_code, uri, expires_in = await lane._get_device_code()
        login_link = uri if uri.startswith("http") else f"https://{uri}"

        # Tidal says how long the link stays valid (5 minutes, lately).
        console.print(
            f"Go to [blue underline]{login_link}[/blue underline] to log into Tidal "
            f"within {expires_in // 60} minutes.",
        )
        _open_login_link(login_link)

        # A few seconds early: Tidal's clock started before its answer arrived.
        deadline = time.time() + expires_in - 5
        while True:
            if time.time() > deadline:
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
        console.print(
            "\nHow do you want to log in to Deezer?\n"
            "  1. Open an isolated browser window that logs in and captures\n"
            "     the ARL cookie automatically\n"
            "  2. Copy the ARL cookie from your browser by hand\n"
        )
        choice = Prompt.ask("Choose", choices=["1", "2"], default="2")

        if choice == "1":
            try:
                arl = await capture_deezer_arl_via_browser()
            except DeezerArlCaptureError as e:
                console.print(f"[yellow]{e}")
            else:
                self.config.session.deezer.arl = arl
                return

        _open_login_link("https://www.deezer.com/login")
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
    """SoundCloud needs no login: its client id is scraped on its own."""

    client: SoundcloudClient

    def has_creds(self) -> bool:
        return True

    async def prompt_and_login(self):
        """Nothing to ask for: SoundCloud needs no login."""
        pass

    def save(self):
        pass


class SpotifyPrompter(CredentialPrompter):
    """Spotify wants an app of the user's own, and a login through the browser."""

    client: SpotifyClient

    def has_creds(self) -> bool:
        c = self.config.session.spotify
        return c.client_id != "" and c.refresh_token != ""

    async def prompt_and_login(self):
        """Ask for the app's client id unless one is saved, then log in."""
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
                return
            except (SpotifyLoginError, AuthenticationError) as e:
                console.print(f"[yellow]{e}")
                if not Confirm.ask("Try again?", default=True):
                    raise AuthenticationError("Spotify login cancelled.") from e

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
            "itself is found on YouTube Music.\n"
        )
        _open_login_link(DEVELOPER_DASHBOARD)

    async def _log_in(self):
        """One browser login: send the user to Spotify and take the code back."""
        url, state, verifier = self.client.authorization_url()
        redirect_uri = self.config.session.spotify.redirect_uri
        console.print(
            "\nHow do you want to log in to Spotify?\n"
            "  1. In the browser on this computer: streamrip catches the login itself\n"
            "  2. In a browser on another device (streamrip runs on a server, say):\n"
            "     open the link there and paste the address you end up on\n"
        )
        choice = Prompt.ask("Choose", choices=["1", "2"], default="1")
        if choice == "1":
            console.print(
                f"\nOpening your browser. If nothing opens, go to:\n{url}\n"
                f"Spotify must send you back to [bold]{redirect_uri}[/bold]; "
                "if it says INVALID_CLIENT, add that as a Redirect URI to your app."
            )
            _open_login_link(url)
            code = await capture_code(redirect_uri, state)
        else:
            console.print(f"\nOpen this link and log in:\n{url}\n")
            console.print(
                "The page you end up on will not load: that is as it should be. "
                "Copy its whole address from the address bar."
            )
            address = Prompt.ask("Paste the address")
            code = code_from_redirect_url(address, state)
        await self.client.finish_login(code, verifier)

    def save(self):
        """Write the session's Spotify app and login to the config file."""
        c = self.config.session.spotify
        cf = self.config.file.spotify
        for name in ("client_id", "access_token", "refresh_token", "token_expiry"):
            setattr(cf, name, getattr(c, name))
        self.config.file.set_modified()
        self._announce_saved()


PROMPTERS = {
    "qobuz": QobuzPrompter,
    "deezer": DeezerPrompter,
    "tidal": TidalPrompter,
    "soundcloud": SoundcloudPrompter,
    "spotify": SpotifyPrompter,
}


def get_prompter(client: Client, config: Config) -> CredentialPrompter:
    """Return an instance of a prompter."""
    p = PROMPTERS[client.source]
    return p(config, client)
