"""The Accounts page: every login in one place, and the actions behind its buttons."""

import math

from flask import Blueprint, Response, abort, current_app, jsonify, request

from ..accounts import BY_ID, LOGIN_IDS, Accounts
from ..client.soloist import SoloistBackend
from ..console import plural
from ..sync.errors import ProviderError
from ..sync.services import DEVELOPER_DASHBOARDS, LABELS
from .common import get_config, get_runner, get_services, payload, sent, spa, start
from .logins import BrowserLogin, ScriptLogin
lazy from .jobs import Job

accounts_pages = Blueprint("accounts", __name__, url_prefix="/accounts")

# The logins that go through the browser, with an app of the user's own.
APP_LOGINS = ("spotify", "tidal-library")
# For the routes: Flask wants names with a hyphen in quotes.
_APP = ", ".join(map(repr, APP_LOGINS))
_ALL = ", ".join(map(repr, LOGIN_IDS))


def _given(name: str) -> str:
    return str(payload().get(name) or "").strip()


def _done(message: str) -> Response:
    return jsonify(message=message)


def _soloist(config) -> dict:
    """What the card of Spotify's own audio says: is it on, set up, paired."""
    c = config.session.spotify
    backend = SoloistBackend(c)
    return {
        "on": c.backend == "spotify",
        "supported": backend.supported,
        "key": bool(c.soloist_api_key),
        "paired": backend.paired(),
        "device": backend.device_name,
    }


@accounts_pages.route("")
@accounts_pages.route("/<any(qobuz, deezer):service>/receive")
def page(service: str = "") -> Response:
    """The page, and the one a login script moves its tab to: there the app
    reads the login after the "#" (it never reaches the server) and hands it
    to script_capture."""
    return spa()


@accounts_pages.get("/api")
def data() -> Response:
    services = get_services()
    config = get_config()
    return jsonify(
        accounts={
            s.login.id: {
                "label": s.login.label,
                "logged_in": s.logged_in,
                "guest": s.login.guest,
                "app_missing": s.app_missing,
            }
            for s in Accounts(config).all()
        },
        dashboards=DEVELOPER_DASHBOARDS,
        redirect_uris={
            login_id: services.redirect_uri(BY_ID[login_id].section)
            for login_id in APP_LOGINS
        },
        soloist=_soloist(config),
        state_path=str(config.state.path),
        tidal_secret=bool(services.client_secret("tidal")),
    )


@accounts_pages.get("/overview")
def overview() -> Response:
    """What every login is and gives, for the lines on the cards: what the last
    check said. The page does not ask the services by itself."""
    return jsonify(get_runner().overview())


@accounts_pages.post("/verify")
def verify() -> tuple[Response, int]:
    """Ask every service whether its login works, unless that was asked a few
    times just now."""
    runner = get_runner()
    if wait := runner.check_wait():
        minutes = plural(math.ceil(wait / 60), "minute")
        abort(
            429,
            "You checked a few times just now. Take it easy: checking too often "
            f"can get an account banned. Try again in {minutes}.",
        )
    return start("verify", "Checking every login", runner.verify)


# --- the logins -------------------------------------------------------------------


@accounts_pages.post("/qobuz/login")
def login_qobuz() -> Response:
    user_id, token = _given("user_id"), _given("auth_token")
    if not user_id or not token:
        raise ProviderError("Fill in both the user id and the token.")
    get_runner().login_qobuz(user_id, token)
    return _done("Logged in to Qobuz.")


@accounts_pages.post("/<any(qobuz, deezer):service>/script")
def script_login(service: str) -> tuple[Response, int]:
    """A login by a script: the page shows it, and run on the service's site it
    hands the login back to script_capture."""
    login = ScriptLogin(service, f"{request.host_url}accounts/{service}/receive")
    logins = current_app.extensions["logins"]
    for earlier in list(logins.values()):
        earlier.cancel()
    runner = get_runner()
    finish = runner.login_qobuz if service == "qobuz" else runner.login_deezer

    def work(job: Job) -> str:
        logins[job.id] = login
        try:
            return login.run(job, finish)
        finally:
            logins.pop(job.id, None)

    return start("login", f"Log in to {login.label} with a script", work)


@accounts_pages.post("/<any(qobuz, deezer):service>/capture")
def script_capture(service: str) -> tuple[Response, int]:
    """The login, from the receiving page. What lets it in is the challenge of a
    login that is waiting: only the script that login showed has it."""
    data = sent()
    taken = any(
        isinstance(login, ScriptLogin)
        and login.service == service
        and login.receive(str(data.get("challenge") or ""), data)
        for login in list(current_app.extensions["logins"].values())
    )
    if not taken:
        raise ProviderError("That was not the script of a login waiting here.")
    return jsonify(status="ok"), 200


@accounts_pages.post("/deezer/login")
def login_deezer() -> Response:
    if not (arl := _given("arl")):
        raise ProviderError("Paste the ARL cookie first.")
    get_runner().login_deezer(arl)
    return _done("Logged in to Deezer.")


@accounts_pages.post("/soundcloud/login")
def login_soundcloud() -> Response:
    if not (token := _given("oauth_token")):
        raise ProviderError("Paste the oauth_token cookie first.")
    get_runner().login_soundcloud(token)
    return _done("Logged in to SoundCloud.")


@accounts_pages.post("/tidal/login")
def login_tidal() -> tuple[Response, int]:
    """Tidal's device login, for downloading."""
    runner = get_runner()
    return start("login", "Log in to Tidal (downloads)", runner.login_tidal)


@accounts_pages.post(f"/<any({_APP}):login_id>/login")
def browser_login(login_id: str) -> tuple[Response, int]:
    service = BY_ID[login_id].section
    services = get_services()
    login = BrowserLogin(services.oauth(service), services.store)
    logins = current_app.extensions["logins"]
    # One login at a time: one given up on would keep the port for minutes.
    for earlier in list(logins.values()):
        earlier.cancel()

    def work(job: Job) -> str:
        logins[job.id] = login
        try:
            return login.run(job)
        finally:
            logins.pop(job.id, None)

    what = "" if service == "spotify" else " (playlists)"
    return start("login", f"Log in to {LABELS[service]}{what}", work)


@accounts_pages.post("/soloist/pair")
def soloist_pair() -> tuple[Response, int]:
    """Spotify's own audio: the Soloist API key, and one pairing."""
    c = get_config().session.spotify
    if c.backend != "spotify":
        raise ProviderError('Spotify\'s own audio is off: backend is "youtube".')
    if not (key := str(sent().get("api_key") or "").strip() or c.soloist_api_key):
        raise ProviderError("Paste the Soloist API key first.")
    runner = get_runner()
    return start(
        "login", "Pair Spotify's own audio", lambda job: runner.pair_soloist(job, key)
    )


@accounts_pages.post(f"/<any({_APP}):login_id>/app")
def set_app(login_id: str) -> Response:
    """The client id (and for Tidal the secret) of the user's developer app."""
    service = BY_ID[login_id].section
    client_id, secret = _given("client_id"), _given("client_secret")
    services, config = get_services(), get_config()
    if not client_id or (
        service == "tidal" and not secret and not services.client_secret("tidal")
    ):
        raise ProviderError(
            "Fill in the Client ID" + (" and Secret." if service == "tidal" else ".")
        )
    config.update_logins(
        service,
        {"client_id": client_id}
        if service == "spotify"
        else {
            "api_client_id": client_id,
            "api_client_secret": secret or config.session.tidal.api_client_secret,
        },
    )
    # A login that is waiting was started with the old app (Qobuz has none).
    for login in list(current_app.extensions["logins"].values()):
        if isinstance(login, BrowserLogin) and login.oauth.name == service:
            login.cancel()
    return _done(f"Saved the {LABELS[service]} app. Now log in.")


@accounts_pages.post(f"/<any({_ALL}):login_id>/logout")
def logout(login_id: str) -> Response:
    forgotten = Accounts(get_config()).logout(login_id)
    label = BY_ID[login_id].label
    return _done(
        f"Logged out of {label}." if forgotten else f"Not logged in to {label}."
    )
