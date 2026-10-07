"""What the user is told, once per run, the first time a source is used."""

from unittest.mock import patch

import pytest

from streamrip.config import Config
from streamrip.rip.main import Main
from streamrip.rip.notices import MUSIC_SYNC, notice_for


def test_spotify_says_it_is_lossy_and_points_to_music_sync():
    notice = notice_for("spotify", Config.defaults())

    assert "lossy" in notice
    assert "YouTube Music" in notice
    assert "Music-Sync" in notice
    assert MUSIC_SYNC == "https://github.com/Stensel8/Music-Sync"
    assert MUSIC_SYNC in notice
    assert "streamrip csv" in notice  # what to do with what Music-Sync exports


def test_deezer_says_it_sends_no_lyrics_when_lyrics_are_wanted():
    config = Config.defaults()

    assert "does not send lyrics" in notice_for("deezer", config)

    config.session.downloads.lyrics = False
    assert notice_for("deezer", config) is None


def test_deezer_says_how_to_get_lyrics_or_that_they_are_looked_up():
    config = Config.defaults()

    # Off: say what to turn on.
    assert "lyrics_fallback = true" in notice_for("deezer", config)

    # On: say where they come from, and nothing left to turn on.
    config.session.downloads.lyrics_fallback = True
    notice = notice_for("deezer", config)
    assert "looked up on lrclib.net" in notice
    assert "lyrics_fallback" not in notice


@pytest.mark.parametrize("source", ["qobuz", "tidal", "soundcloud"])
def test_other_sources_have_nothing_to_say(source):
    assert notice_for(source, Config.defaults()) is None


async def test_a_source_is_announced_once_however_often_it_is_used():
    config = Config.defaults()
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    main = Main(config)
    for client in main.clients.values():
        client.logged_in = True

    with patch("streamrip.rip.main.console") as console:
        for source in ("spotify", "spotify", "deezer", "qobuz", "deezer"):
            await main.get_logged_in_client(source)

    printed = [call.args[0] for call in console.print.call_args_list]
    assert len(printed) == 2
    assert "Spotify" in printed[0]
    assert "Deezer" in printed[1]
