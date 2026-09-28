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
    MiscConfig,
    QobuzConfig,
    SoundcloudConfig,
    TidalConfig,
    _get_dict_keys_r,
    _nested_set,
    update_config,
)

SAMPLE_CONFIG = "tests/test_config.toml"
OLD_CONFIG = "tests/test_config_old.toml"


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


def test_get_keys_r():
    d = {
        "key1": {
            "key2": {
                "key3": 1,
                "key4": 1,
            },
            "key6": [1, 2],
            5: 1,
        }
    }
    res = _get_dict_keys_r(d)
    print(res)
    assert res == {
        ("key1", "key2", "key3"),
        ("key1", "key2", "key4"),
        ("key1", "key6"),
        ("key1", 5),
    }


def test_safe_set():
    d = {
        "key1": {
            "key2": {
                "key3": 1,
                "key4": 1,
            },
            "key6": [1, 2],
            5: 1,
        }
    }
    _nested_set(d, "key1", "key2", "key3", val=5)
    assert d == {
        "key1": {
            "key2": {
                "key3": 5,
                "key4": 1,
            },
            "key6": [1, 2],
            5: 1,
        }
    }


def test_config_update():
    old = {
        "downloads": {"folder": "some_path", "use_service": True},
        "qobuz": {"email": "asdf@gmail.com", "password": "test"},
        "legacy_conf": {"something": 1, "other": 2},
    }
    new = {
        "downloads": {"folder": "", "use_service": False, "keep_artwork": True},
        "qobuz": {"email": "", "password": ""},
        "tidal": {"email": "", "password": ""},
    }
    update_config(old, new)
    assert new == {
        "downloads": {"folder": "some_path", "use_service": True, "keep_artwork": True},
        "qobuz": {"email": "asdf@gmail.com", "password": "test"},
        "tidal": {"email": "", "password": ""},
    }


def test_config_throws_outdated():
    with pytest.raises(Exception, match="update"):
        _ = Config(OLD_CONFIG)


def test_config_file_update():
    tmp_conf = "tests/test_config_old2.toml"
    shutil.copy("tests/test_config_old.toml", tmp_conf)
    Config._update_file(tmp_conf, SAMPLE_CONFIG)

    with open(tmp_conf) as f:
        s = f.read()
        toml = tomlkit.parse(s)  # type: ignore

    assert toml["downloads"]["folder"] == "old_value"  # type: ignore
    assert toml["downloads"]["source_subdirectories"] is True  # type: ignore
    assert toml["downloads"]["max_connections"] == 6  # type: ignore
    assert toml["downloads"]["requests_per_minute"] == 60  # type: ignore
    assert toml["cli"]["progress_bars"] is True  # type: ignore
    assert toml["cli"]["max_search_results"] == 100  # type: ignore
    assert toml["misc"]["version"] == "2.3.3"  # type: ignore
    # Options that no longer exist don't survive the update.
    assert "youtube" not in toml
    assert "text_output" not in toml["cli"]  # type: ignore
    os.remove("tests/test_config_old2.toml")


def test_sample_config_data_properties(sample_config_data):
    # Test the properties of ConfigData
    assert sample_config_data.modified is False  # Ensure initial state is not modified


def test_sample_config_data_modification(sample_config_data):
    # Test modifying ConfigData and checking modified property
    sample_config_data.set_modified()
    assert sample_config_data._modified is True


def test_sample_config_data_fields(sample_config_data):
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
            use_auth_token=False,
            email_or_userid="test@gmail.com",
            password_or_token="test_pwd",
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
        misc=MiscConfig(version="2.0", check_for_updates=True),
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


def test_prefer_explicit_defaults_false():
    assert Config.defaults().session.metadata.prefer_explicit is False


def test_prefer_explicit_missing_from_toml_still_loads():
    # A config saved before this option existed has no `prefer_explicit` key
    # in [metadata] at all; MetadataConfig's default must cover it so those
    # configs keep loading without a version bump.
    with open("streamrip/config.toml") as f:
        toml_str = f.read().replace("\nprefer_explicit = false\n", "\n")
    assert "prefer_explicit" not in toml_str
    data = ConfigData.from_toml(toml_str)
    assert data.metadata.prefer_explicit is False


# Other tests for the Config class can be added as needed

if __name__ == "__main__":
    pytest.main()


def test_merged_options_carry_over_on_update(tmp_path):
    old = tomlkit.parse(open(OLD_CONFIG).read())
    old["downloads"]["concurrency"] = False  # type: ignore
    old["downloads"]["max_connections"] = 6  # type: ignore
    old["qobuz_filters"]["non_studio_albums"] = True  # type: ignore
    old["qobuz_filters"]["repeats"] = True  # type: ignore
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(old))

    Config._update_file(str(path), SAMPLE_CONFIG)

    new = tomlkit.parse(path.read_text())
    # concurrency = false became one download at a time.
    assert new["downloads"]["max_connections"] == 1  # type: ignore
    assert "concurrency" not in new["downloads"]  # type: ignore
    # The filters moved to [artist_filters]; non_studio_albums is part of extras.
    assert "qobuz_filters" not in new
    assert new["artist_filters"]["extras"] is True  # type: ignore
    assert new["artist_filters"]["repeats"] is True  # type: ignore


def _update_with_tidal_login(tmp_path, hires_client: bool):
    old = tomlkit.parse(open(OLD_CONFIG).read())
    tidal = old["tidal"]  # type: ignore
    tidal["hires_client"] = hires_client
    tidal["client_id"] = ""
    tidal["access_token"] = "tok"
    tidal["refresh_token"] = "ref"
    tidal["token_expiry"] = "1"
    tidal["token_client_id"] = "some-client"
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(old))

    Config.update_file(str(path))
    return tomlkit.parse(path.read_text())["tidal"]  # type: ignore


def test_hires_login_moves_to_its_own_fields_on_update(tmp_path):
    # Before there was a second login, hires_client = true replaced the default
    # client and its tokens lived in the ordinary fields.
    tidal = _update_with_tidal_login(tmp_path, hires_client=True)
    assert tidal["hires_access_token"] == "tok"
    assert tidal["hires_refresh_token"] == "ref"
    assert tidal["hires_token_expiry"] == "1"
    assert tidal["hires_token_client_id"] == "some-client"
    assert tidal["access_token"] == ""
    assert tidal["token_client_id"] == ""


def test_default_client_login_stays_put_on_update(tmp_path):
    tidal = _update_with_tidal_login(tmp_path, hires_client=False)
    assert tidal["access_token"] == "tok"
    assert tidal["token_client_id"] == "some-client"
    assert tidal["hires_access_token"] == ""


def test_default_quality_is_the_highest_of_every_source():
    session = Config.defaults().session
    assert session.qobuz.quality == 4  # 24-bit, up to 192 kHz
    assert session.tidal.quality == 3  # best available, hi-res where there is one
    assert session.deezer.quality == 2  # FLAC
