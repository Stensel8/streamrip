"""Classes and functions that manage config state."""

import copy
import functools
import logging
import os
import shutil
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import ClassVar

import click
from tomlkit.api import dumps, parse
from tomlkit.toml_document import TOMLDocument

logger = logging.getLogger("streamrip")

APP_DIR = click.get_app_dir("streamrip")
os.makedirs(APP_DIR, exist_ok=True)
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config.toml")
CURRENT_CONFIG_VERSION = "2.3.2"


class OutdatedConfigError(Exception):
    pass


# Every option is documented in config.toml, the template each user's config
# is made from. These classes only give the options their types.


@dataclass(slots=True)
class QobuzConfig:
    use_auth_token: bool
    email_or_userid: str
    password_or_token: str
    app_id: str
    quality: int
    download_booklets: bool
    secrets: list[str]


@dataclass(slots=True)
class TidalConfig:
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


@dataclass(slots=True)
class DeezerConfig:
    arl: str
    quality: int
    lower_quality_if_not_available: bool


@dataclass(slots=True)
class SoundcloudConfig:
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
    extras: bool
    repeats: bool
    non_albums: bool
    features: bool
    non_remaster: bool


@dataclass(slots=True)
class ArtworkConfig:
    embed: bool
    embed_size: str
    embed_max_width: int
    save_artwork: bool
    saved_max_width: int


@dataclass(slots=True)
class MetadataConfig:
    set_playlist_to_album: bool
    renumber_playlist_tracks: bool
    exclude: list[str]
    prefer_explicit: bool = False


@dataclass(slots=True)
class FilepathsConfig:
    add_singles_to_folder: bool
    folder_format: str
    track_format: str
    restrict_characters: bool
    truncate_to: int


@dataclass(slots=True)
class DownloadsConfig:
    folder: str
    source_subdirectories: bool
    disc_subdirectories: bool
    max_connections: int
    requests_per_minute: int
    verify_ssl: bool
    lyrics: bool = True


@dataclass(slots=True)
class LastFmConfig:
    source: str
    fallback_source: str


@dataclass(slots=True)
class CliConfig:
    progress_bars: bool
    max_search_results: int


@dataclass(slots=True)
class MiscConfig:
    version: str
    check_for_updates: bool


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
    misc: MiscConfig

    _modified: bool = False

    @classmethod
    def from_toml(cls, toml_str: str):
        # TODO: handle the mistake where Windows people forget to escape backslash
        toml = parse(toml_str)
        if (v := toml["misc"]["version"]) != CURRENT_CONFIG_VERSION:  # type: ignore
            raise OutdatedConfigError(
                f"Need to update config from {v} to {CURRENT_CONFIG_VERSION}",
            )
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
        for f in _section_fields():
            update_toml_section_from_config(self.toml[f.name], getattr(self, f.name))

    def get_source(
        self,
        source: str,
    ) -> QobuzConfig | DeezerConfig | SoundcloudConfig | TidalConfig:
        if source not in ("qobuz", "tidal", "deezer", "soundcloud"):
            raise Exception(f"Invalid source {source}")
        return getattr(self, source)


def _section_fields():
    return [f for f in fields(ConfigData) if is_dataclass(f.type)]


def update_toml_section_from_config(toml_section, config):
    for field in fields(config):
        toml_section[field.name] = getattr(config, field.name)


class Config:
    def __init__(self, path: str, /):
        self.path = path

        with open(path) as toml_file:
            self.file: ConfigData = ConfigData.from_toml(toml_file.read())

        self.session: ConfigData = copy.deepcopy(self.file)

    def save_file(self):
        if not self.file.modified:
            return

        with open(self.path, "w") as toml_file:
            self.file.update_toml()
            toml_file.write(dumps(self.file.toml))

    @staticmethod
    def _update_file(old_path: str, new_path: str):
        """Updates the current config based on a newer config `new_toml`."""
        with open(new_path) as new_conf:
            new_toml = parse(new_conf.read())

        toml_set_user_defaults(new_toml)

        with open(old_path) as old_conf:
            old = parse(old_conf.read()).unwrap()

        _carry_over_merged_options(old)
        update_config(old, new_toml)

        with open(old_path, "w") as f:
            f.write(dumps(new_toml))

    @classmethod
    def update_file(cls, path: str):
        cls._update_file(path, BLANK_CONFIG_PATH)

    @classmethod
    def defaults(cls):
        return cls(BLANK_CONFIG_PATH)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.save_file()


def set_user_defaults(path: str, /):
    """Update the TOML file at the path with user-specific default values."""
    shutil.copy(BLANK_CONFIG_PATH, path)

    with open(path) as f:
        toml = parse(f.read())

    toml_set_user_defaults(toml)

    with open(path, "w") as f:
        f.write(dumps(toml))


def toml_set_user_defaults(toml: TOMLDocument):
    toml["downloads"]["folder"] = DEFAULT_DOWNLOADS_FOLDER  # type: ignore
    toml["database"]["downloads_path"] = DEFAULT_DOWNLOADS_DB_PATH  # type: ignore
    toml["database"]["failed_downloads_path"] = DEFAULT_FAILED_DOWNLOADS_DB_PATH  # type: ignore


def _get_dict_keys_r(d: dict) -> set[tuple]:
    """Get all possible key combinations in nested dicts.

    See tests/test_config.py for example.
    """
    keys = d.keys()
    ret = set()
    for cur in keys:
        val = d[cur]
        if isinstance(val, dict):
            ret.update((cur, *remaining) for remaining in _get_dict_keys_r(val))
        else:
            ret.add((cur,))
    return ret


def _nested_get(dictionary, *keys, default=None):
    return functools.reduce(
        lambda d, key: d.get(key, default) if isinstance(d, dict) else default,
        keys,
        dictionary,
    )


def _nested_set(dictionary, *keys, val):
    """Nested set. Throws exception if keys are invalid."""
    assert len(keys) > 0
    final = functools.reduce(lambda d, key: d.get(key), keys[:-1], dictionary)
    final[keys[-1]] = val


def _carry_over_merged_options(old: dict):
    """Rewrite options that were merged into others, or renamed, so that
    update_config() carries their settings over instead of dropping them.
    """
    downloads = old.get("downloads", {})
    # `concurrency = false` meant one download at a time.
    if downloads.get("concurrency") is False:
        downloads["max_connections"] = 1

    if (filters := old.pop("qobuz_filters", None)) is not None:
        # non_studio_albums was extras plus various-artists compilations,
        # which extras now includes.
        if filters.get("non_studio_albums"):
            filters["extras"] = True
        old["artist_filters"] = filters


def update_config(old_with_data: dict, new_without_data: dict):
    """Used to update config when a new config version is detected.

    All data associated with keys that are shared between the old and
    new configs are copied from old to new. The remaining keep their default value.

    Assumes that new_without_data contains default config values of the
    latest version.
    """
    old_keys = _get_dict_keys_r(old_with_data)
    new_keys = _get_dict_keys_r(new_without_data)
    common = old_keys.intersection(new_keys)
    common.discard(("misc", "version"))

    for k in common:
        old_val = _nested_get(old_with_data, *k)
        _nested_set(new_without_data, *k, val=old_val)
