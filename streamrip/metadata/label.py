from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class LabelMetadata:
    name: str
    # The label's album ids.
    ids: list[str]

    @classmethod
    def from_resp(cls, resp: dict, source: str) -> LabelMetadata:
        """A label's name and album ids from a response of `source`."""
        logger.debug(resp)
        if source == "qobuz":
            return cls(resp["name"], [a["id"] for a in resp["albums"]["items"]])
        if source in ("tidal", "deezer"):
            return cls(resp["name"], [a["id"] for a in resp["albums"]])
        raise NotImplementedError
