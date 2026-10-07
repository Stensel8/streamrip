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
    SpotifyConfig,
    TidalConfig,
    set_user_defaults,
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
        spotify=SpotifyConfig(),
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


def test_lyrics_fallback_is_off_by_default_and_explained_in_the_template():
    assert Config.defaults().session.downloads.lyrics_fallback is False
    template = open("streamrip/config.toml").read()
    assert "\nlyrics_fallback = false\n" in template
    assert "lrclib.net" in template


def test_lyrics_fallback_missing_from_toml_still_loads():
    # A config from before the option has no `lyrics_fallback` in [downloads].
    with open("streamrip/config.toml") as f:
        toml_str = f.read().replace("\nlyrics_fallback = false\n", "\n")
    assert "lyrics_fallback" not in toml_str
    assert ConfigData.from_toml(toml_str).downloads.lyrics_fallback is False


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


def test_no_update_check_is_commented_out_in_the_template():
    # Off by default, and visible: a user finds the option by reading the file.
    text = open("streamrip/config.toml").read()

    assert "\n# no_update_check = true\n" in text
    assert "no_update_check" not in tomlkit.parse(text)["cli"]
    assert Config.defaults().session.cli.no_update_check is False


def test_no_update_check_can_be_turned_on_in_the_config(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    path.write_text(
        path.read_text().replace("# no_update_check = true", "no_update_check = true")
    )

    assert Config(str(path)).session.cli.no_update_check is True


def test_saving_does_not_write_out_an_option_the_file_leaves_out(tmp_path):
    # Written beside the commented line, it would make uncommenting that a
    # duplicate key, and the config would not load.
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    config = Config(str(path))
    config.file.deezer.arl = "synthetic-cookie"
    config.file.set_modified()

    config.save_file()

    saved = path.read_text()
    assert "\n# no_update_check = true\n" in saved
    assert "\nno_update_check" not in saved
    path.write_text(saved.replace("# no_update_check = true", "no_update_check = true"))
    assert Config(str(path)).session.cli.no_update_check is True


def test_saving_keeps_an_option_the_user_set(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(str(path))
    path.write_text(
        path.read_text().replace("# no_update_check = true", "no_update_check = true")
    )
    config = Config(str(path))
    config.file.deezer.arl = "synthetic-cookie"
    config.file.set_modified()

    config.save_file()

    assert Config(str(path)).session.cli.no_update_check is True
