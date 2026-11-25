"""Classes and functions that manage config state."""

import copy
import logging
import os
import subprocess
import sys
import threading
from dataclasses import MISSING, dataclass, fields, is_dataclass
from pathlib import Path
from typing import ClassVar

import click
from tomlkit.api import dumps, parse
lazy from tomlkit.toml_document import TOMLDocument

from .state import CREDENTIALS, STATE_NAME, State, blank, make_private, write_private

logger = logging.getLogger("streamrip")

APP_DIR = Path(click.get_app_dir("streamrip"))
APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
if os.name == "posix":
    APP_DIR.chmod(0o700)
DEFAULT_CONFIG_PATH = APP_DIR / "config.toml"
# How fast to rip (see client.client): auto chooses; fast, medium and slow
# insist.
PACES = ("auto", "fast", "medium", "slow")


# Every option is documented in config.toml, the template each user's config
# is made from. These classes only give the options their types.


@dataclass(slots=True, kw_only=True)
class ServiceLimits:
    """How fast streamrip may go on one service. None: streamrip's own numbers
    (client/client.py), measured on Tidal, and [downloads] requests_per_minute."""

    requests_per_minute: int | None = None
    slow_tracks_per_hour: int | None = None
    medium_tracks_per_hour: int | None = None
    max_tracks_per_hour: int | None = None
    full_speed_tracks: int | None = None


@dataclass(slots=True)
class QobuzConfig(ServiceLimits):
    user_id: str
    auth_token: str
    app_id: str
    quality: int
    download_booklets: bool
    secrets: list[str]


@dataclass(slots=True)
class TidalConfig(ServiceLimits):
    """Tidal section of the config file."""

    user_id: str
    country_code: str
    access_token: str
    refresh_token: str
    token_expiry: str
    quality: int
    hires_client: bool
    client_id: str
    client_secret: str
    token_client_id: str
    # The hi-res client's own login, next to the tokens above.
    hires_access_token: str
    hires_refresh_token: str
    hires_token_expiry: str
    hires_token_client_id: str
    # The developer app and login of Tidal's official API, which the playlist
    # tools (streamrip/sync) read and change playlists and favourites with.
    api_client_id: str = ""
    api_client_secret: str = ""
    api_redirect_uri: str = "http://127.0.0.1:9900/tidal/callback"
    api_access_token: str = ""
    api_refresh_token: str = ""
    api_token_expiry: str = ""


@dataclass(slots=True)
class DeezerConfig(ServiceLimits):
    """Deezer section of the config file."""

    arl: str
    quality: int
    lower_quality_if_not_available: bool


@dataclass(slots=True)
class SoundcloudConfig(ServiceLimits):
    """SoundCloud section of the config file."""

    client_id: str
    app_version: str
    # The oauth_token cookie of a soundcloud.com login: better quality and
    # higher limits than a guest has. Kept in state.toml.
    oauth_token: str = ""
    # SoundCloud streams in one quality; nothing to configure.
    quality: ClassVar[int] = 0


@dataclass(slots=True)
class SpotifyConfig(ServiceLimits):
    """Spotify section of the config file."""

    client_id: str
    redirect_uri: str
    # The file format of what is downloaded: "m4a" (AAC) or "mp3".
    audio_format: str
    # kbps, for audio that has to be re-encoded to audio_format.
    audio_bitrate: int
    # Accept a YouTube music video when there is no matching "song".
    match_videos: bool
    access_token: str
    refresh_token: str
    token_expiry: str
    # What the login allows (Spotify says so); the playlist tools need more
    # than downloading does.
    scope: str = ""
    # Where the audio comes from: "spotify" plays the track with Spotify's
    # own headless client (Soloist), at the account's quality, and streamrip
    # records the local audio as lossless FLAC (real time); "youtube" is the
    # same recording matched on YouTube Music. A track the Spotify backend
    # cannot serve is routed to YouTube Music either way.
    backend: str = "spotify"
    # The name of the device streamrip pairs and signs in as.
    device_name: str = "streamrip"
    # The Soloist API key the spotify backend plays with: one per person,
    # generated at developer.spotify.com/dashboard/soloist (a Premium
    # account). A secret, like a password: streamrip fills it in and keeps
    # it here, and Spotify will not say to which account it belongs.
    soloist_api_key: str = ""
    # The quality of the audio, where the source serves it in more than one:
    # 0 is the one quality Spotify has (its own client follows the account
    # tier, YouTube Music its best AAC stream).
    quality: ClassVar[int] = 0


@dataclass(slots=True)
class DatabaseConfig:
    downloads_enabled: bool
    downloads_path: str
    failed_downloads_enabled: bool
    failed_downloads_path: str


@dataclass(slots=True)
class ConversionConfig:
    enabled: bool
    codec: str
    sampling_rate: int
    bit_depth: int
    lossy_bitrate: int


@dataclass(slots=True)
class ArtistFilterConfig:
    """Filters applied when downloading an artist's discography."""

    extras: bool
    repeats: bool
    non_albums: bool
    features: bool
    non_remaster: bool


@dataclass(slots=True)
class ArtworkConfig:
    """Cover art embedding and saving options."""

    embed: bool
    embed_size: str
    embed_max_width: int
    save_artwork: bool
    saved_max_width: int


@dataclass(slots=True)
class MetadataConfig:
    """Tag-writing options that don't fit under a single source."""

    set_playlist_to_album: bool
    renumber_playlist_tracks: bool
    exclude: list[str]
    prefer_explicit: bool
    musicbrainz: bool = True  # a config from before has no such line


@dataclass(slots=True)
class FilepathsConfig:
    """Filename and folder naming options."""

    add_singles_to_folder: bool
    folder_format: str
    track_format: str
    restrict_characters: bool
    truncate_to: int


@dataclass(slots=True)
class DownloadsConfig:
    """Download destination, concurrency, and connection options."""

    folder: str
    source_subdirectories: bool
    disc_subdirectories: bool
    max_connections: int
    requests_per_minute: int
    verify_ssl: bool
    lyrics: bool
    # Not in the config unless the user adds it: see the template.
    pacing: str = "auto"
    create_album_folder: bool = True
    save_lrc: bool = False

    def __post_init__(self):
        if self.pacing not in PACES:
            raise ValueError(f"pacing is {self.pacing!r}: it can be {', '.join(PACES)}")


@dataclass(slots=True)
class LastFmConfig:
    """Last.fm playlist import options."""

    source: str
    fallback_source: str


@dataclass(slots=True)
class CliConfig:
    """Command-line interface display options."""

    progress_bars: bool
    max_search_results: int
    # Not in the config unless the user adds it: see the template.
    no_update_check: bool = False


HOME = Path.home()


def _windows_music_dir() -> Path | None:
    """The user's Music folder from the registry.

    Honours both a move (it may not be under the home folder at all) and a
    non-English Windows, where the real folder is named e.g. "Muziek", not
    "Music" -- only its registry entry says which.
    """
    import winreg  # noqa: PLC0415

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "My Music")
    except OSError:
        return None
    return Path(os.path.expandvars(value)) if value else None


def _xdg_music_dir() -> Path | None:
    """The user's Music folder from xdg-user-dirs, if there is one to ask.

    Same reasoning as _windows_music_dir: honours a move and a non-English
    locale (e.g. "Muziek"), which only xdg-user-dirs' own config knows.
    """
    try:
        found = subprocess.run(
            ["xdg-user-dir", "MUSIC"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    path = found.stdout.strip()
    return Path(path) if found.returncode == 0 and path else None


def _default_music_dir() -> Path:
    """The user's Music folder, asked for the platform-appropriate way.

    macOS keeps it named "Music" on disk whatever the display language, so
    HOME / "Music" is always right there; Windows and Linux can have really
    renamed or moved it, and only ask their own registry/config for the truth.
    """
    if sys.platform == "win32":
        return _windows_music_dir() or HOME / "Music"
    if sys.platform != "darwin":
        return _xdg_music_dir() or HOME / "Music"
    return HOME / "Music"


DEFAULT_DOWNLOADS_FOLDER = _default_music_dir()
DEFAULT_DOWNLOADS_DB_PATH = APP_DIR / "downloads.db"
DEFAULT_FAILED_DOWNLOADS_DB_PATH = APP_DIR / "failed_downloads.db"
BLANK_CONFIG_PATH = Path(__file__).parent / "config.toml"


@dataclass(slots=True)
class ConfigData:
    toml: TOMLDocument
    # One field per [section] of config.toml.
    downloads: DownloadsConfig
    qobuz: QobuzConfig
    tidal: TidalConfig
    deezer: DeezerConfig
    soundcloud: SoundcloudConfig
    spotify: SpotifyConfig
    lastfm: LastFmConfig
    filepaths: FilepathsConfig
    artwork: ArtworkConfig
    metadata: MetadataConfig
    artist_filters: ArtistFilterConfig
    cli: CliConfig
    database: DatabaseConfig
    conversion: ConversionConfig

    _modified: bool = False
    # The options of the file this version does not have, as "[section] name".
    unused: tuple[str, ...] = ()

    @classmethod
    def from_toml(cls, toml_str: str):
        """Parse a config file. Raises if a section or an option it needs is
        missing; an option this version does not have (one an earlier version
        wrote) is left out, and named in `unused`."""
        toml = parse(toml_str)
        sections, unused = {}, []
        for f in _section_fields():
            names = {field.name for field in fields(f.type)}  # type: ignore
            given = dict(toml[f.name])
            unused += (f"[{f.name}] {name}" for name in given if name not in names)
            known = {name: value for name, value in given.items() if name in names}
            sections[f.name] = f.type(**_blank_logins(f.name) | known)  # type: ignore
        return cls(toml=toml, unused=tuple(unused), **sections)

    def set_modified(self):
        self._modified = True

    @property
    def modified(self):
        return self._modified

    def update_toml(self):
        """Write every section's current dataclass values back into self.toml."""
        for f in _section_fields():
            update_toml_section_from_config(
                self.toml[f.name], getattr(self, f.name), CREDENTIALS.get(f.name, ())
            )


def _blank_logins(section: str) -> dict:
    """The logins of a section, as they are while none is stored."""
    return {name: blank(name) for name in CREDENTIALS.get(section, ())}


def _section_fields():
    """Return the ConfigData fields that hold a [section]'s own dataclass."""
    return [f for f in fields(ConfigData) if is_dataclass(f.type)]


def update_toml_section_from_config(toml_section, config, logins=()):
    for field in fields(config):
        if field.name in logins:
            toml_section.pop(field.name, None)  # kept in state.toml
            continue
        value = getattr(config, field.name)
        # An option the file leaves out (commented out in the template) stays
        # out while it has its default. Written back, it would sit beside the
        # commented line, and uncommenting that is a duplicate key.
        if (
            field.name not in toml_section
            and field.default is not MISSING
            and value == field.default
        ):
            continue
        toml_section[field.name] = value


def expand_home(path: str) -> str:
    """A path that starts with "~" as the one in the home folder it means."""
    return str(Path(path).expanduser()) if path.startswith("~") else path


class Config:
    """A user's config, loaded from a TOML file at path."""

    def __init__(self, path: Path, /):
        """Load the config at path, securing it first if it holds secrets."""
        self.path = path

        # utf-8-sig: Notepad and PowerShell 5.1 save "UTF-8" with a BOM, which
        # TOML takes for an empty key.
        with path.open(encoding="utf-8-sig") as toml_file:
            # The packaged template is shared, but user configs contain secrets.
            if os.name == "posix" and not path.samefile(BLANK_CONFIG_PATH):
                make_private(path, toml_file.fileno())
            self.file: ConfigData = ConfigData.from_toml(toml_file.read())
        if self.file.unused:
            logger.warning(
                "Ignoring %s in %s: not an option of this streamrip (a typo, or an old one).",
                ", ".join(self.file.unused),
                path,
            )

        # The logins are in state.toml, next to the config.
        self._template = path.samefile(BLANK_CONFIG_PATH)
        self.state = State(None if self._template else path.with_name(STATE_NAME))
        self._stored = self._load_logins()

        self.session: ConfigData = copy.deepcopy(self.file)
        # "~/Music" is in the home folder, not in a folder named "~" where
        # streamrip happens to be run. The file keeps it as it was written.
        downloads, database = self.session.downloads, self.session.database
        downloads.folder = expand_home(downloads.folder)
        database.downloads_path = expand_home(database.downloads_path)
        database.failed_downloads_path = expand_home(database.failed_downloads_path)
        if not self._template:
            # Empty is the default place, as the template says: not the folder
            # streamrip happens to be run in, nor a database gone after the run.
            downloads.folder = downloads.folder or str(DEFAULT_DOWNLOADS_FOLDER)
            database.downloads_path = database.downloads_path or str(
                path.with_name("downloads.db")
            )
            database.failed_downloads_path = database.failed_downloads_path or str(
                path.with_name("failed_downloads.db")
            )
        # What the file said when it was read, to tell at saving what this run
        # changed in self.file (a new token, a fetched app id).
        self._loaded: ConfigData = copy.deepcopy(self.file)
        self._saving = threading.Lock()
        if not self._template and self._holds_logins():
            self._move_logins_out()

    def _holds_logins(self) -> bool:
        """Whether config.toml itself has logins in it, as before 2.8."""
        return any(
            name in self.file.toml[section]
            for section, names in CREDENTIALS.items()
            for name in names
        )

    def _move_logins_out(self):
        """Move the logins of config.toml to state.toml, at the first run of a
        config from before 2.8. Left in config.toml, they came back after a
        logout, which is saved in state.toml."""
        self.file.set_modified()
        try:
            self.save_file()
        except OSError as e:
            self.file._modified = False  # not tried again at the end of the run
            logger.warning(
                "Could not move the logins in %s to %s: %s", self.path, STATE_NAME, e
            )

    def update_logins(self, section: str, values: dict):
        """Set logins of [section] (a login, a logout) here and in what is
        saved, and save them."""
        for data in (self.session, self.file):
            for name, value in values.items():
                setattr(getattr(data, section), name, copy.deepcopy(value))
        self.save_file()

    def reload_logins(self, section: str):
        """Take on what another program (the web server, a command) saved of
        this section's logins since this one read them. A token that rotates at
        every refresh is dead once the new one is saved: refreshing with the old
        one would fail. Changes made here and not saved yet are left alone."""
        stored = self.state.read().get(section, {})
        for name in CREDENTIALS[section]:
            value = stored.get(name, blank(name))
            if value == self._stored[section][name]:
                continue  # nobody else changed it
            for data in (self.session, self.file):
                setattr(getattr(data, section), name, copy.deepcopy(value))
            self._stored[section][name] = copy.deepcopy(value)

    def _load_logins(self) -> dict[str, dict]:
        """Lay the logins of the state file over self.file. Returns what the
        state file holds of them, to tell at saving what this run changed."""
        stored = self.state.read()
        known = {}
        for section, names in CREDENTIALS.items():
            data = getattr(self.file, section)
            held = stored.get(section, {})
            known[section] = {name: held.get(name, blank(name)) for name in names}
            for name, value in known[section].items():
                if value != blank(name):
                    setattr(data, name, copy.deepcopy(value))
        return known

    def _changed_logins(self) -> dict[str, dict]:
        """The logins this run changed since it loaded or last saved them."""
        changed: dict[str, dict] = {}
        for section, names in CREDENTIALS.items():
            data = getattr(self.file, section)
            for name in names:
                value = getattr(data, name)
                if value != self._stored[section][name]:
                    changed.setdefault(section, {})[name] = copy.deepcopy(value)
        return changed

    def save_file(self):
        """Write what was changed in self.file back to path, unless nothing was.

        Safe from several threads at once (the web interface saves a login
        while a download saves its own).

        Into the file as it is now, not as it was read: saving happens at the
        end of a run, which can be hours later. Writing the whole of self.file
        undid whatever was changed in config.toml meanwhile, by hand or by
        another streamrip saving its own new token.
        """
        if self._template:
            return  # the packaged template is nobody's config to write
        with self._saving:
            self._save_file()

    def _save_file(self):
        # The logins first: they are out of config.toml once that is written.
        if changed := self._changed_logins():
            self.state.update(changed)
            for section, values in changed.items():
                self._stored[section].update(copy.deepcopy(values))
        if not self.file.modified:
            return

        data = self.file
        try:
            with self.path.open(encoding="utf-8-sig") as toml_file:
                current = ConfigData.from_toml(toml_file.read())
        except Exception as e:
            # Gone, or no config this version reads any more: ours, as it is.
            logger.debug("Saving the config over what is at %s: %s", self.path, e)
        else:
            for section in _section_fields():
                ours, loaded, theirs = (
                    getattr(d, section.name) for d in (self.file, self._loaded, current)
                )
                for field in fields(ours):
                    value = getattr(ours, field.name)
                    if value != getattr(loaded, field.name):
                        setattr(theirs, field.name, copy.deepcopy(value))
            data = current
        data.update_toml()
        _write_config(self.path, data.toml)
        # What this run changed is in the file now. A later save compares with
        # that, or a value set back to what it was at loading (a logout, a
        # token cleared) would never be written.
        self._loaded = copy.deepcopy(self.file)
        self.file._modified = False

    @classmethod
    def defaults(cls):
        return cls(BLANK_CONFIG_PATH)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.save_file()


def _write_config(path: Path, toml: TOMLDocument):
    """Write toml to path in one step, private from its first byte (see
    state.write_private)."""
    write_private(path, dumps(toml))


def set_user_defaults(path: Path, /):
    """Update the TOML file at the path with user-specific default values."""
    toml = parse(BLANK_CONFIG_PATH.read_text())
    toml["downloads"]["folder"] = str(DEFAULT_DOWNLOADS_FOLDER)  # type: ignore
    toml["database"]["downloads_path"] = str(DEFAULT_DOWNLOADS_DB_PATH)  # type: ignore
    toml["database"]["failed_downloads_path"] = str(DEFAULT_FAILED_DOWNLOADS_DB_PATH)  # type: ignore
    _write_config(path, toml)
