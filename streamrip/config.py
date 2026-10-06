"""Classes and functions that manage config state."""

import contextlib
import copy
import logging
import os
import tempfile
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import ClassVar

import click
from tomlkit.api import dumps, parse
from tomlkit.toml_document import TOMLDocument

logger = logging.getLogger("streamrip")

APP_DIR = click.get_app_dir("streamrip")
os.makedirs(APP_DIR, mode=0o700, exist_ok=True)
if os.name == "posix":
    os.chmod(APP_DIR, 0o700)
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config.toml")


# Every option is documented in config.toml, the template each user's config
# is made from. These classes only give the options their types.


@dataclass(slots=True)
class QobuzConfig:
    user_id: str
    auth_token: str
    app_id: str
    quality: int
    download_booklets: bool
    secrets: list[str]


@dataclass(slots=True)
class TidalConfig:
    """Tidal section of the config file."""

    user_id: str
    country_code: str
    access_token: str
    refresh_token: str
    token_expiry: str
    quality: int
    hires_client: bool = False
    client_id: str = ""
    client_secret: str = ""
    token_client_id: str = ""
    # The hi-res client's own login, next to the tokens above.
    hires_access_token: str = ""
    hires_refresh_token: str = ""
    hires_token_expiry: str = ""
    hires_token_client_id: str = ""


@dataclass(slots=True)
class DeezerConfig:
    """Deezer section of the config file."""

    arl: str
    quality: int
    lower_quality_if_not_available: bool


@dataclass(slots=True)
class SoundcloudConfig:
    """SoundCloud section of the config file."""

    client_id: str
    app_version: str
    # SoundCloud streams in one quality; nothing to configure.
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
    prefer_explicit: bool = True


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
    lyrics: bool = True


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


HOME = Path.home()
DEFAULT_DOWNLOADS_FOLDER = os.path.join(HOME, "StreamripDownloads")
DEFAULT_DOWNLOADS_DB_PATH = os.path.join(APP_DIR, "downloads.db")
DEFAULT_FAILED_DOWNLOADS_DB_PATH = os.path.join(APP_DIR, "failed_downloads.db")
BLANK_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.toml")
assert os.path.isfile(BLANK_CONFIG_PATH), "Template config not found"


@dataclass(slots=True)
class ConfigData:
    toml: TOMLDocument
    # One field per [section] of config.toml.
    downloads: DownloadsConfig
    qobuz: QobuzConfig
    tidal: TidalConfig
    deezer: DeezerConfig
    soundcloud: SoundcloudConfig
    lastfm: LastFmConfig
    filepaths: FilepathsConfig
    artwork: ArtworkConfig
    metadata: MetadataConfig
    artist_filters: ArtistFilterConfig
    cli: CliConfig
    database: DatabaseConfig
    conversion: ConversionConfig

    _modified: bool = False

    @classmethod
    def from_toml(cls, toml_str: str):
        """Parse a config file. Raises if its schema doesn't match this version.

        Sections this version has no class for, like the [misc] of older
        releases, are left alone.
        """
        # TODO: handle the mistake where Windows people forget to escape backslash
        toml = parse(toml_str)
        sections = {f.name: f.type(**toml[f.name]) for f in _section_fields()}  # type: ignore
        return cls(toml=toml, **sections)

    @classmethod
    def defaults(cls):
        with open(BLANK_CONFIG_PATH) as f:
            return cls.from_toml(f.read())

    def set_modified(self):
        self._modified = True

    @property
    def modified(self):
        return self._modified

    def update_toml(self):
        """Write every section's current dataclass values back into self.toml."""
        for f in _section_fields():
            update_toml_section_from_config(self.toml[f.name], getattr(self, f.name))

    def get_source(
        self,
        source: str,
    ) -> QobuzConfig | DeezerConfig | SoundcloudConfig | TidalConfig:
        """Return the config for the given streaming source."""
        if source not in ("qobuz", "tidal", "deezer", "soundcloud"):
            raise Exception(f"Invalid source {source}")
        return getattr(self, source)


def _section_fields():
    """Return the ConfigData fields that hold a [section]'s own dataclass."""
    return [f for f in fields(ConfigData) if is_dataclass(f.type)]


def update_toml_section_from_config(toml_section, config):
    for field in fields(config):
        toml_section[field.name] = getattr(config, field.name)


class Config:
    """A user's config, loaded from a TOML file at path."""

    def __init__(self, path: str, /):
        """Load the config at path, securing it first if it holds secrets."""
        self.path = path

        with open(path) as toml_file:
            # The packaged template is shared, but user configs contain secrets.
            if os.name == "posix" and not os.path.samefile(path, BLANK_CONFIG_PATH):
                os.fchmod(toml_file.fileno(), 0o600)
            self.file: ConfigData = ConfigData.from_toml(toml_file.read())

        self.session: ConfigData = copy.deepcopy(self.file)

    def save_file(self):
        """Write self.file back to path, unless nothing was modified."""
        if not self.file.modified:
            return

        self.file.update_toml()
        _write_config(self.path, self.file.toml)

    @classmethod
    def defaults(cls):
        return cls(BLANK_CONFIG_PATH)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.save_file()


def _write_config(path: str, toml: TOMLDocument):
    """Write toml to path in one step, private from its first byte.

    The contents go to a temporary file next to it, which replaces the config
    only once it is complete. A crash, a Ctrl-C or a full disk part way
    through leaves the old config, tokens and all, instead of a truncated one.
    """
    contents = dumps(toml)
    # A symlinked config (kept in a dotfiles repo, say) is written through, not
    # replaced by a regular file.
    target = os.path.realpath(path)
    # mkstemp creates the file 0600 whatever the umask, so there is no moment
    # at which the new tokens are readable by others.
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(target), prefix=".config-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            if os.name == "posix":
                os.fchmod(f.fileno(), 0o600)
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_path)
        raise


def set_user_defaults(path: str, /):
    """Update the TOML file at the path with user-specific default values."""
    with open(BLANK_CONFIG_PATH) as f:
        toml = parse(f.read())

    toml_set_user_defaults(toml)

    _write_config(path, toml)


def toml_set_user_defaults(toml: TOMLDocument):
    """Point toml's path-valued options at directories under this user's home."""
    toml["downloads"]["folder"] = DEFAULT_DOWNLOADS_FOLDER  # type: ignore
    toml["database"]["downloads_path"] = DEFAULT_DOWNLOADS_DB_PATH  # type: ignore
    toml["database"]["failed_downloads_path"] = DEFAULT_FAILED_DOWNLOADS_DB_PATH  # type: ignore
