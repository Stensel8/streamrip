import os
import shutil

import pytest
import tomlkit

from streamrip.config import (
    ArtistFilterConfig,
    ArtworkConfig,
    CliConfig,
    Config,
    ConfigData,
    ConversionConfig,
    DatabaseConfig,
    DeezerConfig,
    DownloadsConfig,
    FilepathsConfig,
    LastFmConfig,
    MetadataConfig,
    QobuzConfig,
    SoundcloudConfig,
    TidalConfig,
)

SAMPLE_CONFIG = "tests/test_config.toml"


# Define a fixture to create a sample ConfigData instance for testing
@pytest.fixture()
def sample_config_data() -> ConfigData:
    # Create a sample ConfigData instance here
    # You can customize this to your specific needs for testing
    with open(SAMPLE_CONFIG) as f:
        config_data = ConfigData.from_toml(f.read())
    return config_data


# Define a fixture to create a sample Config instance for testing
@pytest.fixture()
def sample_config() -> Config:
    # Create a sample Config instance here
    # You can customize this to your specific needs for testing
    config = Config(SAMPLE_CONFIG)
    return config


def test_sample_config_data_properties(sample_config_data):
    # Test the properties of ConfigData
    assert sample_config_data.modified is False  # Ensure initial state is not modified


def test_sample_config_data_modification(sample_config_data):
    # Test modifying ConfigData and checking modified property
    sample_config_data.set_modified()
    assert sample_config_data._modified is True


def test_sample_config_data_fields(sample_config_data):
    """A ConfigData written out by hand has the sections of the sample config."""
    test_config = ConfigData(
        toml=None,  # type: ignore
        downloads=DownloadsConfig(
            folder="test_folder",
            source_subdirectories=False,
            disc_subdirectories=True,
            max_connections=6,
            requests_per_minute=60,
            verify_ssl=True,
            lyrics=True,
        ),
        qobuz=QobuzConfig(
            user_id="123456789",
            auth_token="test_token",
            app_id="12345",
            quality=3,
            download_booklets=True,
            secrets=["secret1", "secret2"],
        ),
        tidal=TidalConfig(
            user_id="userid",
            country_code="countrycode",
            access_token="accesstoken",
            refresh_token="refreshtoken",
            token_expiry="tokenexpiry",
            quality=3,
        ),
        deezer=DeezerConfig(
            arl="testarl",
            quality=2,
            lower_quality_if_not_available=True,
        ),
        soundcloud=SoundcloudConfig(
            client_id="clientid",
            app_version="appversion",
        ),
        lastfm=LastFmConfig(source="qobuz", fallback_source=""),
        filepaths=FilepathsConfig(
            add_singles_to_folder=False,
            folder_format="{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]",
            track_format="{tracknumber}. {artist} - {title}{explicit}",
            restrict_characters=False,
            truncate_to=120,
        ),
        artwork=ArtworkConfig(
            embed=True,
            embed_size="large",
            embed_max_width=-1,
            save_artwork=True,
            saved_max_width=-1,
        ),
        metadata=MetadataConfig(
            set_playlist_to_album=True,
            renumber_playlist_tracks=True,
            exclude=[],
        ),
        artist_filters=ArtistFilterConfig(
            extras=False,
            repeats=False,
            non_albums=False,
            features=False,
            non_remaster=False,
        ),
        cli=CliConfig(
            progress_bars=False,
            max_search_results=100,
        ),
        database=DatabaseConfig(
            downloads_enabled=True,
            downloads_path="downloadspath",
            failed_downloads_enabled=True,
            failed_downloads_path="faileddownloadspath",
        ),
        conversion=ConversionConfig(
            enabled=False,
            codec="ALAC",
            sampling_rate=48000,
            bit_depth=24,
            lossy_bitrate=320,
        ),
        _modified=False,
    )
    assert sample_config_data.downloads == test_config.downloads
    assert sample_config_data.qobuz == test_config.qobuz
    assert sample_config_data.tidal == test_config.tidal
    assert sample_config_data.deezer == test_config.deezer
    assert sample_config_data.soundcloud == test_config.soundcloud
    assert sample_config_data.lastfm == test_config.lastfm
    assert sample_config_data.artwork == test_config.artwork
    assert sample_config_data.filepaths == test_config.filepaths
    assert sample_config_data.metadata == test_config.metadata
    assert sample_config_data.artist_filters == test_config.artist_filters
    assert sample_config_data.database == test_config.database
    assert sample_config_data.conversion == test_config.conversion


def test_config_update_on_save():
    tmp_config_path = "tests/config2.toml"
    shutil.copy(SAMPLE_CONFIG, tmp_config_path)
    conf = Config(tmp_config_path)
    conf.file.downloads.folder = "new_folder"
    conf.file.set_modified()
    conf.save_file()
    conf2 = Config(tmp_config_path)
    os.remove(tmp_config_path)

    assert conf2.session.downloads.folder == "new_folder"


def test_config_dont_update_without_set_modified():
    tmp_config_path = "tests/config2.toml"
    shutil.copy(SAMPLE_CONFIG, tmp_config_path)
    conf = Config(tmp_config_path)
    conf.file.downloads.folder = "new_folder"
    del conf
    conf2 = Config(tmp_config_path)
    os.remove(tmp_config_path)

    assert conf2.session.downloads.folder == "test_folder"


def test_prefer_explicit_defaults_true():
    assert Config.defaults().session.metadata.prefer_explicit is True


def test_prefer_explicit_missing_from_toml_still_loads():
    # A config saved before this option existed has no `prefer_explicit` key
    # in [metadata] at all; MetadataConfig's default must cover it so those
    # configs keep loading without a version bump.
    with open("streamrip/config.toml") as f:
        toml_str = f.read().replace("\nprefer_explicit = true\n", "\n")
    assert "prefer_explicit" not in toml_str
    data = ConfigData.from_toml(toml_str)
    assert data.metadata.prefer_explicit is True


# Other tests for the Config class can be added as needed

if __name__ == "__main__":
    pytest.main()


def test_default_quality_is_the_highest_of_every_source():
    session = Config.defaults().session
    assert session.qobuz.quality == 4  # 24-bit, up to 192 kHz
    assert session.tidal.quality == 3  # best available, hi-res where there is one
    assert session.deezer.quality == 2  # FLAC


def test_old_misc_section_still_loads():
    """A config with an old [misc] section still loads.

    Older releases wrote it: a schema version, and a check_for_updates switch the update
    check never read.
    """
    with open(SAMPLE_CONFIG) as f:
        doc = tomlkit.parse(f.read())
    doc["misc"]["version"] = "2.3.3"
    data = ConfigData.from_toml(tomlkit.dumps(doc))
    data.update_toml()
    assert data.toml["misc"]["version"] == "2.3.3"
