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
from ..client import Client
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filename, clean_filepath
from ..metadata import (
    AlbumMetadata,
    Covers,
    PlaylistMetadata,
    SearchResults,
    TrackMetadata,
)
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .artwork import download_artwork
from .media import Media, Pending, rip_tracks
from .track import Track

logger = logging.getLogger("streamrip")

# Tracks of a playlist resolved at once. Playlist tracks come from different
# albums, so each resolve also fetches a cover; more than this only delays
# the first download.
RESOLVE_CONCURRENCY = 20


@dataclass(slots=True)
class PendingPlaylistTrack(Pending):
    id: str
    client: Client
    config: Config
    folder: str
    playlist_name: str
    position: int
    db: Database

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(f"Track ({self.id}) already logged in database. Skipping.")
            return None

        source = self.client.source
        # As in PendingTrack.resolve: record every failure, so a track that
        # dies here is retryable by `streamrip repair` instead of vanishing.
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Could not stream track {self.id}: {e}")
            self.db.set_failed(source, "track", self.id)
            return None

        meta = None
        try:
            album = AlbumMetadata.from_track_resp(resp, source)
            if album is not None:
                meta = TrackMetadata.from_resp(album, source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for {self.id}: {e}")
            self.db.set_failed(source, "track", self.id)
            return None

        if meta is None:
            logger.error(f"Track ({self.id}) not available for stream on {source}")
            self.db.set_failed(source, "track", self.id)
            return None

        c = self.config.session.metadata
        if c.renumber_playlist_tracks:
            meta.tracknumber = self.position
        if c.set_playlist_to_album:
            album.album = self.playlist_name

        quality = self.config.session.get_source(self.client.source).quality
        try:
            embedded_cover_path, downloadable = await asyncio.gather(
                self._download_cover(album.covers, self.folder),
                self.client.get_downloadable(self.id, quality),
            )
        except NonStreamableError as e:
            logger.error(f"Error fetching download info for track {self.id}: {e}")
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        return Track(
            meta,
            downloadable,
            self.config,
            self.folder,
            embedded_cover_path,
            self.db,
        )

    async def _download_cover(self, covers: Covers, folder: str) -> str | None:
        embed_path, _ = await download_artwork(
            self.client.session,
            folder,
            covers,
            self.config.session.artwork,
            for_playlist=True,
        )
        return embed_path


@dataclass(slots=True)
class Playlist(Media):
    name: str
    config: Config
    client: Client
    tracks: list[PendingPlaylistTrack]

    async def preprocess(self):
        progress.add_title(self.name)

    async def postprocess(self):
        progress.remove_title(self.name)

    async def download(self):
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
        parent = self.config.session.downloads.folder
        folder = os.path.join(parent, clean_filepath(clean_filename(name)))
        tracks = [
            PendingPlaylistTrack(
                id,
                self.client,
                self.config,
                folder,
                name,
                position + 1,
                self.db,
            )
            for position, id in enumerate(meta.ids())
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

        parent = self.config.session.downloads.folder
        folder = os.path.join(parent, clean_filepath(clean_filename(playlist_title)))

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

        Each page contains 50 results, so `num_tracks // 50 + 1` requests
        are sent per playlist.

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

        # Create new session so we're not bound by rate limit
        verify_ssl = getattr(self.config.session.downloads, "verify_ssl", True)
        connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
        connector = aiohttp.TCPConnector(
            **connector_kwargs, resolver=aiohttp.ThreadedResolver()
        )

        async with aiohttp.ClientSession(
            connector=connector, trust_env=True
        ) as session:
            page = await fetch(session, playlist_url)
            playlist_title_match = re_playlist_title_match.search(page)
            if playlist_title_match is None:
                raise Exception("Error finding title from response")

            playlist_title: str = html.unescape(playlist_title_match.group(1))

            title_artist_pairs: list[tuple[str, str]] = find_title_artist_pairs(page)

            total_tracks_match = re_total_tracks.search(page)
            if total_tracks_match is None:
                raise Exception("Error parsing lastfm page: %s", page)
            total_tracks = int(total_tracks_match.group(1))

            remaining_tracks = total_tracks - 50  # already got 50 from 1st page
            if remaining_tracks <= 0:
                return playlist_title, title_artist_pairs

            last_page = (
                1 + int(remaining_tracks // 50) + int(remaining_tracks % 50 != 0)
            )
            requests = []
            for page in range(2, last_page + 1):
                requests.append(fetch(session, playlist_url, params={"page": page}))
            results = await asyncio.gather(*requests)

        for page in results:
            title_artist_pairs.extend(find_title_artist_pairs(page))

        return playlist_title, title_artist_pairs
