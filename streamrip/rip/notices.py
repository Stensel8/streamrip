"""What to tell the user, once per run, the first time they use a source."""


def notice_for(source: str) -> str | None:
    """Rich markup for what the user should know about `source`, or None."""
    if source == "spotify":
        return (
            "[yellow]Spotify:[/yellow] the audio comes from YouTube Music, so it is "
            "lossy. For lossless, download your Spotify playlists from Qobuz, Tidal "
            "or Deezer instead: [bold]streamrip playlist download spotify --playlist "
            "NAME --source qobuz[/bold] (or [bold]--liked[/bold]). [bold]streamrip "
            "playlist transfer spotify tidal[/bold] moves them to Tidal for good."
        )
    return None
