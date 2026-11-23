"""What to tell the user, once per run, the first time they use a source."""

MUSIC_SYNC = "https://github.com/Stensel8/Music-Sync"


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
            "playlist transfer spotify tidal[/bold] moves them to Tidal for good."
        )
    return (
        "[yellow]Spotify:[/yellow] the audio comes from Spotify itself: its "
        "official headless client plays each track here in real time and "
        "streamrip records it from the local audio, so it is the account's own "
        "quality -- lossless FLAC on Premium. A track it cannot play is found "
        "on YouTube Music instead, and is lossy. For lossless everywhere, move "
        "your playlists to Tidal, or export them to a CSV, with Music-Sync, and "
        "download them from there ([bold]streamrip csv list.csv[/bold]):\n  "
        f"[link={MUSIC_SYNC}]{MUSIC_SYNC}[/link]"
    )
