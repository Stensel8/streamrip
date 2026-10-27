import asyncio
import html
import logging
import re
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
lazy import aiohttp

from .. import progress
from ..client import Client, new_session
from ..client.audio_match import MatchTrack, rank, score, simplify_title
from ..console import console
from ..filepath_utils import clean_filename
from ..metadata import (
    AlbumMetadata,
    PlaylistMetadata,
    SearchResults,
    Summary,
    TrackMetadata,
)
from .artwork import download_artwork
from .media import Media, Pending, PendingItem, rip_tracks
from .track import Track, fetch_downloadable, fetch_track_meta
lazy from ..config import Config
lazy from ..db import Database

logger = logging.getLogger("streamrip")

# Tracks of a playlist resolved at once. Playlist tracks come from different
# albums, so each resolve also fetches a cover; more than this only delays
# the first download.
RESOLVE_CONCURRENCY = 20

# Local work budget for untrusted Last.fm counts: at most 200 pages of 50.
LASTFM_MAX_TRACKS = 10_000

# A list's track is searched for: from this score on a search result is the
# track (the threshold of Music-Sync), and this many results are looked at.
MIN_SCORE = 0.8
SEARCH_LIMIT = 5


def _playlist_folder(config: Config, name: str) -> Path:
    """Return the sanitized download folder for a playlist named name.

    Raises ValueError if the sanitized name is empty, ".", "..", or would
    resolve outside the downloads root.
    """
    c = config.session
    folder = clean_filename(name, c.filepaths.restrict_characters)
    if folder in {"", ".", ".."}:
        raise ValueError(f"Invalid playlist folder: {name!r}")
    root = Path(c.downloads.folder).resolve()
    path = (root / folder).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"Invalid playlist folder outside download root: {name!r}")
    return path


def clock_ms(text: str | None) -> int | None:
    """ "3:45" or "1:02:03" in milliseconds, or None for anything else.

    A bare number could be seconds or milliseconds, so it says nothing.
    """
    if not text or not re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        return None
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def _as_track(result: Summary) -> MatchTrack:
    """A search result as a track to compare the wanted one with."""
    details = dict(result.details)
    return MatchTrack(
        result.name,
        [result.artist] if result.artist else [],
        details.get("Album", ""),
        clock_ms(details.get("Length")),
    )


def best_result(
    wanted: MatchTrack, results: list[Summary]
) -> tuple[Summary, float] | None:
    """The search result that is `wanted` and its score, if one is good enough."""
    candidates = [(result, _as_track(result)) for result in results]
    best = max(candidates, key=lambda c: rank(wanted, c[1]), default=None)
    if best is None or (found := score(wanted, best[1])) < MIN_SCORE:
        return None
    return best[0], found


def queries(wanted: MatchTrack) -> list[str]:
    """What to search for: the title without brackets and "feat." first, which is
    how a catalogue most often has it, then the title as the list has it, then
    the title before the artist: some catalogues rank that better (Qobuz has the
    original of "Get Lucky" in the first results of "Get Lucky Daft Punk" only).
    """
    title = simplify_title(wanted.title)
    return list(
        dict.fromkeys(
            f"{first} {second}".strip()
            for first, second in [
                (wanted.artist, title),
                (wanted.artist, wanted.title),
                (title, wanted.artist),
            ]
        )
    )


async def find_track(
    wanted: MatchTrack, clients: list[Client]
) -> tuple[Client, Summary] | None:
    """Search each client in turn for a track: the first that has it, and its
    result there."""
    for client in clients:
        for query in queries(wanted):
            try:
                pages = await client.search("track", query, limit=SEARCH_LIMIT)
                results = SearchResults.from_pages(client.source, "track", pages)
            except Exception as e:
                # One failed search is no reason to give up the list.
                logger.warning(
                    "Searching %s for %r failed: %s", client.source, query, e
                )
                continue
            if (hit := best_result(wanted, results.results)) is not None:
                logger.debug(
                    "%s is %r on %s (score %.2f, searched for %r)",
                    wanted,
                    hit[0].name,
                    client.source,
                    hit[1],
                    query,
                )
                return client, hit[0]
    return None


async def searched_playlist(
    name: str,
    tracks: list[MatchTrack],
    client: Client,
    fallback_client: Client | None,
    config: Config,
    db: Database,
) -> Playlist | None:
    """A playlist of the tracks found by searching client (and fallback_client
    for what it lacks), in a folder named name; None if none was found."""
    try:
        folder = _playlist_folder(config, name)
    except ValueError as e:
        logger.error("Error creating playlist: %s", e)
        return None

    clients = [c for c in (client, fallback_client) if c is not None]
    found = failed = 0

    def status() -> Text:
        return Text.assemble(
            f"Searching {client.source.title()} for the tracks (",
            (f"{found} found", "bold green"),
            ", ",
            (f"{failed} not found", "bold red"),
            ", ",
            (f"{len(tracks)} total", "bold"),
            ")",
        )

    show = config.session.cli.progress_bars
    with console.status(status(), spinner="moon") if show else nullcontext() as spin:

        async def find(wanted: MatchTrack):
            """The (client, result) for one track, or None; keeps the count."""
            nonlocal found, failed
            hit = await find_track(wanted, clients)
            if hit is None:
                failed += 1
                logger.warning("Not found: %s", wanted)
            else:
                found += 1
            if spin is not None:
                spin.update(status())
            return hit

        hits = await asyncio.gather(*map(find, tracks))

    pending: list[PendingPlaylistTrack] = []
    seen: set[tuple[str, str]] = set()
    for position, hit in enumerate(hits, start=1):
        if hit is None:
            continue
        source_client, result = hit
        # A track listed twice must not be downloaded twice at the same time.
        if (source_client.source, result.id) in seen:
            logger.info("Listed twice, downloaded once: %s", tracks[position - 1])
            continue
        seen.add((source_client.source, result.id))
        pending.append(
            PendingPlaylistTrack(
                result.id,
                source_client,
                config,
                folder,
                name,
                position,
                db,
                total=len(tracks),
            )
        )
    logger.info("%s: found %s of %s tracks", name, found, len(tracks))
    if not pending:
        logger.error("None of the tracks of %s was found", name)
        return None
    return Playlist(name, config, client, pending)


@dataclass(slots=True)
class PendingPlaylistTrack(Pending):
    id: str
    client: Client
    config: Config
    folder: Path
    playlist_name: str
    position: int
    db: Database
    # Number of tracks in the playlist, for the track total when renumbering.
    total: int = 0
    meta: TrackMetadata | None = None

    async def look_up(self) -> TrackMetadata | None:
        """This track's metadata, fetched once; None if it's not to download."""
        if self.meta is None:
            self.meta = await fetch_track_meta(self.client, self.db, self.id)
        return self.meta

    async def resolve(self) -> Track | None:
        """Fetch metadata and cover art, and resolve into a downloadable Track."""
        meta = await self.look_up()
        if meta is None:
            return None
        downloadable = await fetch_downloadable(
            self.client, self.config, self.db, self.id
        )
        if downloadable is None:
            return None

        # Deezer serves a geoblocked or delisted track from another release,
        # the one FALLBACK.SNG_ID names; the downloadable's id is then that
        # track's. The requested track's metadata no longer describes the
        # bytes -- its album, and its cover: for such releases Deezer only has
        # a grey "no cover" placeholder (an unknown picture hash redirects to
        # d41d8cd98f00b204e9800998ecf8427e). Use the served track's metadata.
        # The cover is fetched only now, once it is known which track's it is.
        served = getattr(downloadable, "id", None)
        if served is not None and str(served) != str(self.id):
            meta = await self._served_track_meta(str(served)) or meta
            # Tags and cover come from the served track; the database, `repair`
            # and the summary know the playlist's entry, so they keep its id.
            meta.info.id = self.id

        album, c = meta.album, self.config.session
        if c.metadata.renumber_playlist_tracks:
            # Disc and total come from the track's own album; left alone, a
            # disc-2 track sorts after the rest and "5/12" in a 50-track list.
            meta.tracknumber, meta.discnumber = self.position, 1
            album.tracktotal, album.disctotal = self.total or album.tracktotal, 1
        if c.metadata.set_playlist_to_album:
            # Music servers group albums by album artist as well, so each
            # track's own would split the playlist into one album per artist
            # (upstream PR #738).
            album.album = self.playlist_name
            album.albumartist, album.albumartists = "Various Artists", None
            album.compilation = "1"

        cover_path, _ = await download_artwork(
            self.client.session,
            self.folder,
            album.covers,
            c.artwork,
            for_playlist=True,
        )
        return Track(
            meta,
            downloadable,
            self.config,
            self.folder,
            cover_path,
            self.db,
            self.client,
        )

    async def _served_track_meta(self, served_id: str) -> TrackMetadata | None:
        """The metadata of the track actually served, or None to keep the original.

        Not fetch_track_meta: the served id must not be checked against or
        recorded in the database, which only knows the requested one.
        """
        source = self.client.source
        try:
            resp = await self.client.get_metadata(served_id, "track")
            album = AlbumMetadata.from_track_resp(resp, source)
            meta = album and TrackMetadata.from_resp(album, source, resp)
        except Exception as e:
            logger.warning(
                "Track %s is served from %s, whose metadata could not be read "
                "(%s: %s); keeping the requested track's metadata",
                self.id,
                served_id,
                type(e).__name__,
                e,
            )
            return None
        if meta is not None:
            logger.debug(
                "Track %s served from fallback %s; using its metadata and cover",
                self.id,
                served_id,
            )
        return meta


@dataclass(slots=True)
class Playlist(Media):
    name: str
    config: Config
    client: Client
    tracks: list[PendingPlaylistTrack]

    async def preprocess(self):
        """Register the playlist's title for progress display."""
        progress.add_title(id(self), self.name, self.config.session.cli.progress_bars)

    async def postprocess(self):
        """Remove the playlist's title from progress display."""
        progress.remove_title(id(self), self.config.session.cli.progress_bars)

    async def download(self):
        """Resolve and download every track of the playlist."""
        if len(self.tracks) > RESOLVE_CONCURRENCY:
            console.log(f"Resolving {len(self.tracks)} tracks: {self.name}")
        await rip_tracks(
            self.tracks,
            RESOLVE_CONCURRENCY,
            self.config.session.metadata.prefer_explicit,
            self.config.session.cli.progress_bars,
        )


@dataclass(slots=True)
class PendingPlaylist(PendingItem):
    async def resolve(self) -> Playlist | None:
        """Fetch the playlist's metadata and resolve into a Playlist of tracks."""
        if (resp := await self._fetch("playlist")) is None:
            return None
        try:
            meta = PlaylistMetadata.from_resp(resp, self.client.source)
            folder = _playlist_folder(self.config, meta.name)
        except Exception as e:
            logger.error("Error creating playlist: %s", e)
            return None
        tracks = [
            PendingPlaylistTrack(
                track_id,
                self.client,
                self.config,
                folder,
                meta.name,
                position,
                self.db,
                total=len(meta.ids),
            )
            for position, track_id in enumerate(meta.ids, start=1)
        ]
        return Playlist(meta.name, self.config, self.client, tracks)


@dataclass(slots=True)
class PendingLastfmPlaylist(Pending):
    """A Last.fm playlist, its tracks searched on a source (see searched_playlist)."""

    lastfm_url: str
    client: Client
    fallback_client: Client | None
    config: Config
    db: Database

    async def resolve(self) -> Playlist | None:
        """Read the playlist from Last.fm, then find its tracks."""
        try:
            title, titles_artists = await self._parse_lastfm_playlist(self.lastfm_url)
        except Exception as e:
            logger.error("Error occurred while parsing last.fm page: %s", e)
            return None
        tracks = [MatchTrack(t, [a]) for t, a in titles_artists]
        return await searched_playlist(
            title, tracks, self.client, self.fallback_client, self.config, self.db
        )

    async def _parse_lastfm_playlist(
        self,
        playlist_url: str,
    ) -> tuple[str, list[tuple[str, str]]]:
        """From a last.fm url, return the playlist title, and a list of
        track titles and artist names.

        Each page contains 50 results. Playlists above LASTFM_MAX_TRACKS
        are rejected, and pages are fetched one at a time to bound pending work.
        """
        logger.debug("Fetching lastfm playlist")

        title_tags = re.compile(r'<a\s+href="[^"]+"\s+title="([^"]+)"')
        re_total_tracks = re.compile(r'data-playlisting-entry-count="(\d+)"')
        re_playlist_title_match = re.compile(
            r'<h1 class="playlisting-playlist-header-title">([^<]+)</h1>',
        )

        def find_title_artist_pairs(page_text) -> list[tuple[str, str]]:
            """The (title, artist) pairs of a last.fm playlist page."""
            # Each track is a link with its title, then one with its artist.
            titles = [html.unescape(t) for t in title_tags.findall(page_text)]
            return list(zip(titles[::2], titles[1::2], strict=False))

        async def fetch(session: aiohttp.ClientSession, url, **kwargs):
            async with session.get(url, **kwargs) as resp:
                return await resp.text("utf-8")

        # A session of its own, so these requests don't count against the
        # client's rate limit.
        verify_ssl = self.config.session.downloads.verify_ssl
        async with new_session(verify_ssl=verify_ssl) as session:
            page = await fetch(session, playlist_url)
            playlist_title_match = re_playlist_title_match.search(page)
            if playlist_title_match is None:
                raise ValueError("no playlist title on the last.fm page")

            playlist_title: str = html.unescape(playlist_title_match.group(1))

            total_tracks_match = re_total_tracks.search(page)
            if total_tracks_match is None:
                raise ValueError("no track count on the last.fm page")
            total_tracks = int(total_tracks_match.group(1))
            if total_tracks > LASTFM_MAX_TRACKS:
                raise ValueError(
                    f"Last.fm playlist exceeds the supported limit of "
                    f"{LASTFM_MAX_TRACKS} tracks"
                )

            title_artist_pairs = find_title_artist_pairs(page)
            last_page = (total_tracks + 49) // 50
            for page_number in range(2, last_page + 1):
                page = await fetch(session, playlist_url, params={"page": page_number})
                title_artist_pairs.extend(find_title_artist_pairs(page))

        return playlist_title, title_artist_pairs
