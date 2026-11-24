"""`streamrip verify`: try every login against its service.

`streamrip accounts` says what is saved; this asks the services, and says what
to do about what does not work.
"""

import asyncio
from dataclasses import dataclass

from .accounts import BY_ID, LOGIN_IDS, Accounts
from .exceptions import AuthenticationError, MissingCredentialsError
from .sync.errors import NotLoggedInError
lazy from .client import DeezerClient, QobuzClient, SoundcloudClient, TidalClient
lazy from .config import Config
lazy from .sync.services import Services

TIMEOUT = 20  # seconds a service gets to answer

OK, NOT_LOGGED_IN, NEEDS_APP, FAILED = "ok", "not logged in", "needs an app", "failed"
GUEST = "guest"  # works without a login (SoundCloud)


@dataclass(frozen=True, slots=True)
class Result:
    id: str
    state: str  # OK, NOT_LOGGED_IN, NEEDS_APP or FAILED
    detail: str  # what the account is, or why it is not working
    fix: str = ""  # what to do about it
    hint: str = ""  # something to know, though it works

    @property
    def fixable(self) -> bool:
        return self.state in {NOT_LOGGED_IN, FAILED} and self.fix.startswith(
            "streamrip login"
        )


async def _download_login(config: Config, source: str) -> tuple[str, str]:
    """Log in as a download does, and ask what the account may do."""
    client = {
        "qobuz": QobuzClient,
        "tidal": TidalClient,
        "deezer": DeezerClient,
        "soundcloud": SoundcloudClient,
    }[source](config)
    try:
        await client.login()
        if reason := getattr(client, "guest_reason", ""):
            raise AuthenticationError(reason)  # a saved login that is not in use
        plan, best = await client.account_plan()
    finally:
        await client.close()
    # What was asked, not what a client lowered it to for the run.
    quality = getattr(
        client, "requested_quality", getattr(config.session, source).quality
    )
    hint = ""
    if best is not None and quality > best:
        hint = (
            f"[{source}] quality = {quality} asks for more than {plan} gives: "
            f"set it to {best}"
        )
    return plan, hint


async def _library_login(config: Config, name: str) -> tuple[str, str]:
    provider = await asyncio.to_thread(Services(config).provider, name)
    return await asyncio.to_thread(provider.account), ""


async def _check(config: Config, login_id: str) -> tuple[str, str]:
    if login_id in {"tidal-library", "spotify"}:
        return await _library_login(config, BY_ID[login_id].section)
    return await _download_login(config, login_id)


async def _verify_one(config: Config, accounts: Accounts, login_id: str) -> Result:
    status = accounts.status(login_id)
    if status.app_missing:
        return Result(
            login_id,
            NEEDS_APP,
            "make an app of your own first",
            f"streamrip login {login_id}",
        )
    if not status.logged_in:
        if status.login.guest:
            return Result(
                login_id,
                GUEST,
                "works without a login; one gives better quality and higher limits",
                f"streamrip login {login_id}",
            )
        return Result(login_id, NOT_LOGGED_IN, "", f"streamrip login {login_id}")
    try:
        detail, hint = await asyncio.wait_for(_check(config, login_id), TIMEOUT)
    except (AuthenticationError, MissingCredentialsError, NotLoggedInError) as e:
        # The error already says "Run: streamrip login x"; the fix is shown apart.
        why = str(e).split(" Run: ")[0]
        return Result(login_id, FAILED, why, f"streamrip login {login_id}")
    except TimeoutError:
        return Result(login_id, FAILED, f"no answer in {TIMEOUT} s")
    except Exception as e:  # not a login problem: a network or service one
        return Result(login_id, FAILED, str(e) or type(e).__name__)
    return Result(login_id, OK, detail, hint=hint)


async def verify(config: Config, ids: tuple[str, ...] = LOGIN_IDS) -> list[Result]:
    """Check the logins in `ids` at the same time."""
    accounts = Accounts(config)
    try:
        return await asyncio.gather(*(_verify_one(config, accounts, i) for i in ids))
    finally:
        config.save_file()  # a login that was renewed on the way is kept
