import logging
import os

import pytest
from util import arun

from streamrip.client.downloadable import BasicDownloadable
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.exceptions import MissingCredentialsError
from streamrip.metadata.util import get_album_track_ids

logger = logging.getLogger("streamrip")


def test_client_raises_missing_credentials():
    c = Config.defaults()
    with pytest.raises(MissingCredentialsError):
        arun(QobuzClient(c).login())


@pytest.mark.skipif(
    not (os.environ.get("QOBUZ_USER_ID") and os.environ.get("QOBUZ_AUTH_TOKEN")),
    reason="Qobuz user ID and auth token are required.",
)
def test_client_get_metadata(qobuz_client):
    meta = arun(qobuz_client.get_metadata("0603497941032", "album"))
    assert (meta["title"], meta["version"]) == ("Rumours", "2001 Remaster")
    # album/get lists the track ids, not the tracks (July 2026).
    assert len(get_album_track_ids("qobuz", meta)) == meta["tracks_count"] == 11
    assert meta["maximum_bit_depth"] == 24


@pytest.mark.skipif(
    not (os.environ.get("QOBUZ_USER_ID") and os.environ.get("QOBUZ_AUTH_TOKEN")),
    reason="Qobuz user ID and auth token are required.",
)
def test_client_get_downloadable(qobuz_client):
    d = arun(qobuz_client.get_downloadable("19512574", 3))
    assert isinstance(d, BasicDownloadable)
    assert d.extension == "flac"
    assert isinstance(d.url, str)
    assert "https://" in d.url


@pytest.mark.skipif(
    not (os.environ.get("QOBUZ_USER_ID") and os.environ.get("QOBUZ_AUTH_TOKEN")),
    reason="Qobuz user ID and auth token are required.",
)
def test_client_search_limit(qobuz_client):
    res = qobuz_client.search("album", "rumours", limit=5)
    total = 0
    for r in arun(res):
        total += len(r["albums"]["items"])
    assert total == 5


@pytest.mark.skipif(
    not (os.environ.get("QOBUZ_USER_ID") and os.environ.get("QOBUZ_AUTH_TOKEN")),
    reason="Qobuz user ID and auth token are required.",
)
def test_client_search_no_limit(qobuz_client):
    # Setting no limit has become impossible because `limit: int` now
    res = qobuz_client.search("album", "rumours", limit=10000)
    correct_total = 0
    total = 0
    for r in arun(res):
        total += len(r["albums"]["items"])
        correct_total = max(correct_total, r["albums"]["total"])
    assert total == correct_total
