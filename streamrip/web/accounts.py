"""The Accounts page: every login in one place, and the actions behind its buttons."""

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    url_for,
)
lazy from werkzeug.wrappers import Response as WerkzeugResponse

from ..accounts import BY_ID, LOGIN_IDS, Accounts
from ..sync.services import DEVELOPER_DASHBOARDS, LABELS
from .common import form, get_config, get_runner, get_services, start
from .logins import BrowserLogin, DeviceLogin
lazy from .jobs import Job

accounts_pages = Blueprint("accounts", __name__, url_prefix="/accounts")

# The logins that go through the browser, with an app of the user's own.
APP_LOGINS = ("spotify", "tidal-library")
# For the routes: Flask wants names with a hyphen in quotes.
_APP = ", ".join(map(repr, APP_LOGINS))
_ALL = ", ".join(map(repr, LOGIN_IDS))


def _back(message: str, category: str = "ok") -> WerkzeugResponse:
    flash(message, category)
    return redirect(url_for("accounts.page"))


@accounts_pages.route("")
def page() -> str:
    services = get_services()
    config = get_config()
    accounts = {s.login.id: s for s in Accounts(config).all()}
    return render_template(
        "accounts.html",
        accounts=accounts,
        dashboards=DEVELOPER_DASHBOARDS,
        redirect_uris={
            login_id: services.redirect_uri(BY_ID[login_id].section)
            for login_id in APP_LOGINS
        },
        state_path=config.state.path,
        tidal_secret=bool(services.client_secret("tidal")),
    )


@accounts_pages.post("/verify")
def verify() -> tuple[Response, int]:
    """Ask every service whether its login works."""
    runner = get_runner()
    return start("verify", "Checking every login", runner.verify)


# --- the logins -------------------------------------------------------------------


@accounts_pages.post("/qobuz/login")
def login_qobuz() -> WerkzeugResponse:
    user_id, token = form("user_id"), form("auth_token")
    if not user_id or not token:
        return _back("Fill in both the user id and the token.", "error")
    get_runner().login_qobuz(user_id, token)
    return _back("Logged in to Qobuz.")


@accounts_pages.post("/deezer/login")
def login_deezer() -> WerkzeugResponse:
    if not (arl := form("arl")):
        return _back("Paste the ARL cookie first.", "error")
    get_runner().login_deezer(arl)
    return _back("Logged in to Deezer.")


@accounts_pages.post("/soundcloud/login")
def login_soundcloud() -> WerkzeugResponse:
    if not (token := form("oauth_token")):
        return _back("Paste the oauth_token cookie first.", "error")
    get_runner().login_soundcloud(token)
    return _back("Logged in to SoundCloud.")


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


@accounts_pages.post("/spotify/pair")
def spotify_pair() -> tuple[Response, int]:
    """Spotify's own desktop client: a pair link, approved on any device, no
    app of one's own."""
    login = DeviceLogin(get_config())
    logins = current_app.extensions["logins"]
    # One login at a time: a device login waits a long time.
    for earlier in list(logins.values()):
        earlier.cancel()

    def work(job: Job) -> str:
        logins[job.id] = login
        try:
            return login.run(job)
        finally:
            logins.pop(job.id, None)

    return start("login", "Log in to Spotify with the pair link", work)


@accounts_pages.post(f"/<any({_APP}):login_id>/app")
def set_app(login_id: str) -> WerkzeugResponse:
    """The client id (and for Tidal the secret) of the user's developer app."""
    service = BY_ID[login_id].section
    client_id, secret = form("client_id"), form("client_secret")
    services, config = get_services(), get_config()
    if not client_id or (
        service == "tidal" and not secret and not services.client_secret("tidal")
    ):
        return _back(
            "Fill in the Client ID" + (" and Secret." if service == "tidal" else "."),
            "error",
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
    # A login that is waiting was started with the old app. A pair link
    # belongs to Spotify's own client, and keeps going.
    for login in list(current_app.extensions["logins"].values()):
        if isinstance(login, BrowserLogin) and login.oauth.name == service:
            login.cancel()
    return _back(f"Saved the {LABELS[service]} app. Now log in.")


@accounts_pages.post(f"/<any({_ALL}):login_id>/logout")
def logout(login_id: str) -> WerkzeugResponse:
    forgotten = Accounts(get_config()).logout(login_id)
    label = BY_ID[login_id].label
    return _back(
        f"Logged out of {label}." if forgotten else f"Not logged in to {label}."
    )
