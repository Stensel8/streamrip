from streamrip.metadata.search_results import ArtistSummary


def test_preview_omits_album_count_when_source_does_not_provide_it():
    # Tidal's artist search response has no album-count field at all, so
    # this used to always show a made-up "Unknown Albums".
    summary = ArtistSummary.from_item({"id": 123, "name": "The Kid LAROI"})
    assert summary.num_albums is None
    assert summary.preview() == "ID: 123"


def test_preview_shows_album_count_when_source_provides_it():
    summary = ArtistSummary.from_item(
        {"id": 456, "name": "Fleetwood Mac", "albums_count": 17}
    )
    assert summary.preview() == "17 Albums\n\nID: 456"
