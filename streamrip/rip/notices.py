"""What to tell the user, once per run, the first time they use a source."""

from ..config import Config

MUSIC_SYNC = "https://github.com/Stensel8/Music-Sync"


def notice_for(source: str, config: Config) -> str | None:
    """Rich markup for what the user should know about `source`, or None."""
    if source == "spotify":
        return (
            "[yellow]Spotify:[/yellow] the audio comes from YouTube Music, so it is "
            "lossy. For better quality, first move your playlists to Tidal, or "
            "export them to a CSV, with Music-Sync, and download them from there "
            "([bold]streamrip csv list.csv[/bold] searches a CSV on Qobuz, Tidal or "
            f"Deezer):\n  [link={MUSIC_SYNC}]{MUSIC_SYNC}[/link]"
        )
    if source == "deezer" and config.session.downloads.lyrics:
        return (
            "[yellow]Deezer:[/yellow] Deezer has no lyrics for every track, so the "
            "others are looked up on lrclib.net, which is told their artist, title "
            "and length. \\[downloads] lyrics = false in the config turns that off."
        )
    return None
