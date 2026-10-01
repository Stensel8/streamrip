import asyncio
import logging
import time
from abc import ABC, abstractmethod

from click import launch

from ..client import Client, DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from ..config import Config
from ..console import console
from ..exceptions import AuthenticationError, MissingCredentialsError
from .interactive import Prompt
from .qobuz_token_capture import (
    QobuzTokenCaptureError,
    capture_qobuz_auth_token,
    capture_qobuz_auth_token_via_browser,
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
        c = self.config.session.qobuz
        c.user_id = user_id
        c.auth_token = token
        console.print(
            f"[green]Credentials will be saved to [bold cyan]{self.config.path}",
        )

    def save(self):
        c = self.config.session.qobuz
        cf = self.config.file.qobuz
        cf.user_id = c.user_id
        cf.auth_token = c.auth_token
        self.config.file.set_modified()


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
        if not self.has_creds():
            self._prompt_creds_and_set_session_config()
        while True:
            try:
                await self.client.login()
                break
            except AuthenticationError:
                console.print("[yellow]Invalid arl, try again.")
                self._prompt_creds_and_set_session_config()
        self.save()

    def _prompt_creds_and_set_session_config(self):
        console.print(
            "If you're not sure how to find the ARL cookie, see the instructions at ",
            "[blue underline]https://github.com/nathom/streamrip/wiki/Finding-your-Deezer-ARL-Cookie",
        )
        c = self.config.session.deezer
        c.arl = Prompt.ask("Enter your [bold]ARL")

    def save(self):
        c = self.config.session.deezer
        cf = self.config.file.deezer
        cf.arl = c.arl
        self.config.file.set_modified()
        console.print(
            f"[green]Credentials saved to config file at [bold cyan]{self.config.path}",
        )


class SoundcloudPrompter(CredentialPrompter):
    """SoundCloud needs no login: its client id is scraped on its own."""

    client: SoundcloudClient

    def has_creds(self) -> bool:
        return True

    async def prompt_and_login(self):
        pass

    def save(self):
        pass


PROMPTERS = {
    "qobuz": QobuzPrompter,
    "deezer": DeezerPrompter,
    "tidal": TidalPrompter,
    "soundcloud": SoundcloudPrompter,
}


def get_prompter(client: Client, config: Config) -> CredentialPrompter:
    """Return an instance of a prompter."""
    p = PROMPTERS[client.source]
    return p(config, client)
