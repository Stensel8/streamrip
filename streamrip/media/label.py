import logging
from dataclasses import dataclass

from ..client import Client
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..metadata import LabelMetadata
from .album import PendingAlbum
from .artist import rip_albums
from .media import Media, Pending

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Label(Media):
    """A record label's catalog: a list of albums."""

    name: str
    albums: list[PendingAlbum]
    client: Client
    config: Config

    async def preprocess(self):
        pass

    async def download(self):
        """Resolve and download every album in the label's catalog."""
        # Fetching each album's tracklist happens a few at a time before the
        # first progress bar appears, which for a label with a large
        # catalog can take a while with nothing on screen to show for it.
        console.print(
            f"[bold]{self.name}[/bold]: found {len(self.albums)} release(s), "
            "resolving and downloading..."
        )
        await rip_albums(self.albums)

    async def postprocess(self):
        pass


@dataclass(slots=True)
class PendingLabel(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Label | None:
        try:
            resp = await self.client.get_metadata(self.id, "label")
        except NonStreamableError as e:
            logger.error(f"Error resolving Label: {e}")
            return None
        try:
            meta = LabelMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error resolving Label: {e}")
            return None
        albums = [
            PendingAlbum(album_id, self.client, self.config, self.db)
            for album_id in meta.album_ids()
        ]
        return Label(meta.name, albums, self.client, self.config)
