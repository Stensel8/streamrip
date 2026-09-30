import asyncio
import hashlib
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
        self.client = self.type_check_client(client)

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

    @abstractmethod
    def type_check_client(self, client: Client):
        raise NotImplementedError


class QobuzPrompter(CredentialPrompter):
    client: QobuzClient

    def has_creds(self) -> bool:
        c = self.config.session.qobuz
        return c.email_or_userid != "" and c.password_or_token != ""

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
        """Ask for a user id + user_auth_token (or, as a fallback, a password).

        Qobuz moved its web login behind OAuth/reCAPTCHA, so the old
        email/password flow fails for most accounts (upstream #954, #956).
        The token from a logged-in browser session still works; offer the
        two ways to capture it automatically (see qobuz_token_capture)
        before falling back to asking for it outright.
        """
        console.print(
            "\n[cyan]Qobuz now requires a token login.[/cyan]\n"
            "How do you want to log in?\n"
            "  1. Open an isolated browser window that logs in and captures\n"
            "     the token automatically\n"
            "  2. Log in in your own browser, then paste a short script into\n"
            "     its console to send the token back\n"
            "  3. Enter the token (or email/password) by hand\n"
        )
        choice = Prompt.ask("Choose", choices=["1", "2", "3"], default="2")

        if choice == "1":
            try:
                user_id, token = await capture_qobuz_auth_token_via_browser()
            except QobuzTokenCaptureError as e:
                console.print(f"[yellow]{e}")
            else:
                self._set_session_creds(True, user_id, token)
                return
        elif choice == "2":
            _open_login_link("https://play.qobuz.com/login")
            try:
                user_id, token = await capture_qobuz_auth_token()
            except QobuzTokenCaptureError as e:
                console.print(f"[yellow]{e}")
            else:
                self._set_session_creds(True, user_id, token)
                return

        _open_login_link("https://play.qobuz.com/login")
        console.print(
            "\nEnter it manually instead:\n"
            "  1. In the browser tab that just opened, open DevTools -> Network,\n"
            "     then log in (log out first if needed)\n"
            "  2. Find the [bold]user/login[/bold] request and open its response\n"
            "  3. Copy [bold]user.id[/bold] and [bold]user_auth_token[/bold]\n"
            "Leave the user id empty to log in with email and password instead.\n"
        )
        user_id = Prompt.ask("Enter your Qobuz user id", default="").strip()
        if user_id:
            token = Prompt.ask(
                "Enter your Qobuz user_auth_token (invisible)", password=True
            ).strip()
            self._set_session_creds(True, user_id, token)
            return

        email = Prompt.ask("Enter your Qobuz email")
        pwd_input = Prompt.ask("Enter your Qobuz password (invisible)", password=True)
        pwd = hashlib.md5(pwd_input.encode("utf-8")).hexdigest()
        self._set_session_creds(False, email, pwd)

    def _set_session_creds(self, use_auth_token: bool, user: str, secret: str):
        c = self.config.session.qobuz
        c.use_auth_token = use_auth_token
        c.email_or_userid = user
        c.password_or_token = secret
        console.print(
            f"[green]Credentials will be saved to [bold cyan]{self.config.path}",
        )

    def save(self):
        c = self.config.session.qobuz
        cf = self.config.file.qobuz
        cf.use_auth_token = c.use_auth_token
        cf.email_or_userid = c.email_or_userid
        cf.password_or_token = c.password_or_token
        self.config.file.set_modified()

    def type_check_client(self, client) -> QobuzClient:
        assert isinstance(client, QobuzClient)
        return client


class TidalPrompter(CredentialPrompter):
    timeout_s: int = 600  # 5 mins to login
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
        if len(self.client.lanes()) > 1:
            console.print(
                "Tidal serves hi-res and CD quality through two separate logins. "
                f"This one is for {'CD quality' if lane is self.client else 'hi-res'}."
            )
        device_code, uri = await lane._get_device_code()
        login_link = uri if uri.startswith("http") else f"https://{uri}"

        console.print(
            f"Go to [blue underline]{login_link}[/blue underline] to log into Tidal "
            f"within {self.timeout_s // 60} minutes.",
        )
        _open_login_link(login_link)

        start = time.time()
        info: dict = {}
        while True:
            if time.time() - start > self.timeout_s:
                raise AuthenticationError("Timed out waiting for the Tidal login.")
            status, info = await lane._get_auth_status(device_code)
            if status == 2:
                # pending
                await asyncio.sleep(4)
                continue
            if status == 0:
                # successful
                break
            raise AuthenticationError(
                "Tidal rejected the device login. Try again, or check the "
                "[tidal] client settings in the config."
            )

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

    def type_check_client(self, client) -> TidalClient:
        assert isinstance(client, TidalClient)
        return client

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

    def type_check_client(self, client) -> DeezerClient:
        assert isinstance(client, DeezerClient)
        return client


class SoundcloudPrompter(CredentialPrompter):
    def has_creds(self) -> bool:
        return True

    async def prompt_and_login(self):
        pass

    def save(self):
        pass

    def type_check_client(self, client) -> SoundcloudClient:
        assert isinstance(client, SoundcloudClient)
        return client


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
