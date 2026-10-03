import logging
from dataclasses import dataclass

from ..client import Client
from ..config import Config
from ..db import Database
from ..exceptions import NonStreamableError
from ..metadata import LabelMetadata
from .album import PendingAlbum
from .artist import announce, rip_albums
from .media import Media, Pending

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Label(Media):
    """A record label's catalog: a list of albums."""

    name: str
    albums: list[PendingAlbum]
    client: Client
    config: Config

    async def download(self):
        """Resolve and download every album in the label's catalog."""
        announce(self.name, self.albums)
        enabled = self.config is not None and self.config.session.cli.progress_bars
        await rip_albums(self.albums, self.name, enabled)


@dataclass(slots=True)
class PendingLabel(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Label | None:
        """Fetch the label and all its albums; None if that fails."""
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
            for album_id in meta.ids
        ]
        return Label(meta.name, albums, self.client, self.config)
