"""What to tell the user, once per run, the first time they use a source."""


def notice_for(source: str, backend: str | None = None) -> str | None:
    """Rich markup for what the user should know about `source`, or None.

    `backend` is the [spotify] backend, for the note that depends on it.
    """
    if source != "spotify":
        return None
    if backend == "youtube":
        return (
            "[yellow]Spotify:[/yellow] the audio comes from YouTube Music, so it is "
            "lossy. For lossless, download your Spotify playlists from Qobuz, Tidal "
            "or Deezer instead: [bold]streamrip playlist download spotify --playlist "
            "NAME --source qobuz[/bold] (or [bold]--liked[/bold]). [bold]streamrip "
            "playlist transfer spotify tidal[/bold] moves them to Tidal for good. "
            "Spotify's own audio needs a Soloist API key: see docs/spotify.md."
        )
    return (
        "[yellow]Spotify:[/yellow] the audio is Spotify's own, at the account's "
        "quality, and a track takes about as long as it lasts. One that does not "
        "work is found on YouTube Music instead, and is lossy. To have a "
        "playlist from Qobuz, Tidal or Deezer instead: [bold]streamrip playlist "
        "download spotify --playlist NAME --source qobuz[/bold]."
    )
