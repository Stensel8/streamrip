import asyncio
import html
import logging
import os
import re
from contextlib import nullcontext
from dataclasses import dataclass

import aiohttp
from rich.text import Text

from .. import progress
from ..client import Client, new_session
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filename
from ..metadata import PlaylistMetadata, SearchResults
from .artwork import download_artwork
from .media import Media, Pending, rip_tracks
from .track import Track, fetch_downloadable, fetch_track_meta

logger = logging.getLogger("streamrip")

# Tracks of a playlist resolved at once. Playlist tracks come from different
# albums, so each resolve also fetches a cover; more than this only delays
# the first download.
RESOLVE_CONCURRENCY = 20

# Local work budget for untrusted Last.fm counts: at most 200 pages of 50.
LASTFM_MAX_TRACKS = 10_000


def _playlist_folder(config: Config, name: str) -> str:
    c = config.session
    folder = clean_filename(name, c.filepaths.restrict_characters)
    return os.path.join(c.downloads.folder, folder)


@dataclass(slots=True)
class PendingPlaylistTrack(Pending):
    id: str
    client: Client
    config: Config
    folder: str
    playlist_name: str
    position: int
    db: Database
    # Number of tracks in the playlist, for the track total when renumbering.
    total: int = 0

    async def resolve(self) -> Track | None:
        meta = await fetch_track_meta(self.client, self.db, self.id)
        if meta is None:
            return None
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
            album.albumartist = "Various Artists"
            album.compilation = "1"

        (cover_path, _), downloadable = await asyncio.gather(
            download_artwork(
                self.client.session,
                self.folder,
                album.covers,
                c.artwork,
                for_playlist=True,
            ),
            fetch_downloadable(self.client, self.config, self.db, self.id),
        )
        if downloadable is None:
            return None
        return Track(meta, downloadable, self.config, self.folder, cover_path, self.db)


@dataclass(slots=True)
class Playlist(Media):
    name: str
    config: Config
    client: Client
    tracks: list[PendingPlaylistTrack]

    async def preprocess(self):
        progress.add_title(id(self), self.name, self.config.session.cli.progress_bars)

    async def postprocess(self):
        progress.remove_title(id(self), self.config.session.cli.progress_bars)

    async def download(self):
        big = len(self.tracks) > RESOLVE_CONCURRENCY
        if big:
            console.log(f"Resolving {len(self.tracks)} tracks: {self.name}")
        enabled = big and self.config.session.cli.progress_bars
        with progress.get_resolve_callback(
            enabled, f"Obtaining playlist info: {self.name}"
        ):
            await rip_tracks(
                self.tracks,
                RESOLVE_CONCURRENCY,
                self.config.session.metadata.prefer_explicit,
            )


@dataclass(slots=True)
class PendingPlaylist(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Playlist | None:
        try:
            resp = await self.client.get_metadata(self.id, "playlist")
        except NonStreamableError as e:
            logger.error(
                f"Playlist {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = PlaylistMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error creating playlist: {e}")
            return None
        name = meta.name
        folder = _playlist_folder(self.config, name)
        ids = meta.ids()
        tracks = [
            PendingPlaylistTrack(
                id,
                self.client,
                self.config,
                folder,
                name,
                position,
                self.db,
                total=len(ids),
            )
            for position, id in enumerate(ids, start=1)
        ]
        return Playlist(name, self.config, self.client, tracks)


@dataclass(slots=True)
class PendingLastfmPlaylist(Pending):
    lastfm_url: str
    client: Client
    fallback_client: Client | None
    config: Config
    db: Database

    @dataclass(slots=True)
    class Status:
        found: int
        failed: int
        total: int

        def text(self) -> Text:
            return Text.assemble(
                "Searching for last.fm tracks (",
                (f"{self.found} found", "bold green"),
                ", ",
                (f"{self.failed} failed", "bold red"),
                ", ",
                (f"{self.total} total", "bold"),
                ")",
            )

    async def resolve(self) -> Playlist | None:
        try:
            playlist_title, titles_artists = await self._parse_lastfm_playlist(
                self.lastfm_url,
            )
        except Exception as e:
            logger.error("Error occurred while parsing last.fm page: %s", e)
            return None

        s = self.Status(0, 0, len(titles_artists))
        show = self.config.session.cli.progress_bars
        with (
            console.status(s.text(), spinner="moon") if show else nullcontext() as spin
        ):

            def callback():
                if spin is not None:
                    spin.update(s.text())

            results = await asyncio.gather(
                *(self._make_query(f"{t} {a}", s, callback) for t, a in titles_artists)
            )

        folder = _playlist_folder(self.config, playlist_title)

        pending_tracks = []
        for pos, (id, from_fallback) in enumerate(results, start=1):
            if id is None:
                logger.warning(f"No results found for {titles_artists[pos - 1]}")
                continue

            if from_fallback:
                assert self.fallback_client is not None
                client = self.fallback_client
            else:
                client = self.client

            pending_tracks.append(
                PendingPlaylistTrack(
                    id,
                    client,
                    self.config,
                    folder,
                    playlist_title,
                    pos,
                    self.db,
                    total=len(results),
                ),
            )

        return Playlist(playlist_title, self.config, self.client, pending_tracks)

    async def _make_query(
        self, query: str, status: Status, callback
    ) -> tuple[str | None, bool]:
        """Search the main source, then the fallback, for one track.

        Returns the first hit's ID (None if nothing matched) and whether it
        came from the fallback source. A search that errors counts as no hit,
        so one bad query doesn't sink the whole playlist.
        """
        hit: tuple[str | None, bool] = (None, False)
        for client, is_fallback in ((self.client, False), (self.fallback_client, True)):
            if client is None:
                continue
            try:
                pages = await client.search("track", query, limit=1)
                found = SearchResults.from_pages(client.source, "track", pages)
            except Exception as e:
                logger.warning(f"Searching {client.source} for {query!r} failed: {e}")
                continue
            if found.results:
                logger.debug(f"Found result for {query} on {client.source}")
                hit = (found.results[0].id, is_fallback)
                break

        if hit[0] is None:
            logger.debug(f"No result found for {query}")
            status.failed += 1
        else:
            status.found += 1
        callback()
        return hit

    async def _parse_lastfm_playlist(
        self,
        playlist_url: str,
    ) -> tuple[str, list[tuple[str, str]]]:
        """From a last.fm url, return the playlist title, and a list of
        track titles and artist names.

        Each page contains 50 results. Playlists above LASTFM_MAX_TRACKS
        are rejected, and pages are fetched one at a time to bound pending work.

        :param url:
        :type url: str
        :rtype: tuple[str, list[tuple[str, str]]]
        """
        logger.debug("Fetching lastfm playlist")

        title_tags = re.compile(r'<a\s+href="[^"]+"\s+title="([^"]+)"')
        re_total_tracks = re.compile(r'data-playlisting-entry-count="(\d+)"')
        re_playlist_title_match = re.compile(
            r'<h1 class="playlisting-playlist-header-title">([^<]+)</h1>',
        )

        def find_title_artist_pairs(page_text):
            info: list[tuple[str, str]] = []
            titles = title_tags.findall(page_text)  # [2:]
            for i in range(0, len(titles) - 1, 2):
                info.append((html.unescape(titles[i]), html.unescape(titles[i + 1])))
            return info

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
                raise Exception("Error finding title from response")

            playlist_title: str = html.unescape(playlist_title_match.group(1))

            total_tracks_match = re_total_tracks.search(page)
            if total_tracks_match is None:
                raise Exception("Error parsing lastfm page: %s", page)
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
