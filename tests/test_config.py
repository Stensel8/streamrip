import re
import shutil
from pathlib import Path

import pytest

from streamrip.config import (
    BLANK_CONFIG_PATH,
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

SAMPLE_CONFIG = Path("tests/test_config.toml")


@pytest.fixture()
def sample_config_data() -> ConfigData:
    return ConfigData.from_toml(SAMPLE_CONFIG.read_text())


# Define a fixture to create a sample Config instance for testing


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
            hires_client=False,
            client_id="",
            client_secret="",
            token_client_id="",
            hires_access_token="",
            hires_refresh_token="",
            hires_token_expiry="",
            hires_token_client_id="",
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
        spotify=SpotifyConfig(
            client_id="",
            redirect_uri="http://127.0.0.1:9900/callback",
            audio_format="m4a",
            audio_bitrate=256,
            match_videos=True,
            access_token="",
            refresh_token="",
            token_expiry="",
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
            prefer_explicit=True,
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


def test_config_update_on_save(tmp_path):
    tmp_config_path = tmp_path / "config.toml"
    shutil.copy(SAMPLE_CONFIG, tmp_config_path)
    conf = Config(tmp_config_path)
    conf.file.downloads.folder = "new_folder"
    conf.file.set_modified()
    conf.save_file()
    conf2 = Config(tmp_config_path)

    assert conf2.session.downloads.folder == "new_folder"


def test_config_dont_update_without_set_modified(tmp_path):
    tmp_config_path = tmp_path / "config.toml"
    shutil.copy(SAMPLE_CONFIG, tmp_config_path)
    conf = Config(tmp_config_path)
    conf.file.downloads.folder = "new_folder"
    del conf
    conf2 = Config(tmp_config_path)

    assert conf2.session.downloads.folder == "test_folder"


def test_the_template_and_the_config_classes_agree():
    """A key the classes lack, or a field the template lacks, fails loading."""
    template = BLANK_CONFIG_PATH.read_text()
    ConfigData.from_toml(template)

    with pytest.raises(TypeError, match="bogus_option"):
        ConfigData.from_toml(
            template.replace("[cli]\n", "[cli]\nbogus_option = 1\n", 1)
        )
    with pytest.raises(TypeError, match="max_search_results"):
        ConfigData.from_toml(
            re.sub(r"^max_search_results = .*\n", "", template, flags=re.MULTILINE)
        )


# Other tests for the Config class can be added as needed


def test_no_update_check_can_be_turned_on_in_the_config(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    path.write_text(
        path.read_text().replace("# no_update_check = true", "no_update_check = true")
    )

    assert Config(path).session.cli.no_update_check is True


def test_saving_does_not_write_out_an_option_the_file_leaves_out(tmp_path):
    # Written beside the commented line, it would make uncommenting that a
    # duplicate key, and the config would not load.
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    config = Config(path)
    config.file.deezer.arl = "synthetic-cookie"
    config.file.set_modified()

    config.save_file()

    saved = path.read_text()
    assert "\n# no_update_check = true\n" in saved
    assert "\nno_update_check" not in saved
    path.write_text(saved.replace("# no_update_check = true", "no_update_check = true"))
    assert Config(path).session.cli.no_update_check is True


def test_saving_keeps_an_option_the_user_set(tmp_path):
    path = tmp_path / "config.toml"
    set_user_defaults(path)
    path.write_text(
        path.read_text().replace("# no_update_check = true", "no_update_check = true")
    )
    config = Config(path)
    config.file.deezer.arl = "synthetic-cookie"
    config.file.set_modified()

    config.save_file()

    assert Config(path).session.cli.no_update_check is True
