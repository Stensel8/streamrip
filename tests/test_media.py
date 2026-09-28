from streamrip.media.media import filter_prefer_explicit
from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
)


class FakeResolvedTrack:
    """Stands in for a resolved `Track`: filter_prefer_explicit only looks at
    `.meta`, so a real `Track` (with a live downloadable, config, db, ...)
    would be unnecessary weight here.
    """

    def __init__(self, title, artist, explicit):
        info = TrackInfo(id="1", quality=2, explicit=explicit)
        album = AlbumMetadata(
            AlbumInfo("1", 2, "flac"), "Album", artist, "2020", [], Covers(), 1
        )
        self.meta = TrackMetadata(
            info=info,
            title=title,
            album=album,
            artist=artist,
            tracknumber=1,
            discnumber=1,
            composer=None,
        )


def test_prefer_explicit_drops_clean_duplicate():
    clean = FakeResolvedTrack("Song", "Artist", False)
    explicit = FakeResolvedTrack("Song", "Artist", True)
    assert filter_prefer_explicit([clean, explicit]) == [explicit]


def test_prefer_explicit_keeps_clean_only():
    clean = FakeResolvedTrack("Song", "Artist", False)
    assert filter_prefer_explicit([clean]) == [clean]


def test_prefer_explicit_keeps_explicit_only():
    explicit = FakeResolvedTrack("Song", "Artist", True)
    assert filter_prefer_explicit([explicit]) == [explicit]


def test_prefer_explicit_leaves_unrelated_tracks_alone():
    clean = FakeResolvedTrack("Song", "Artist", False)
    explicit = FakeResolvedTrack("Song", "Artist", True)
    other = FakeResolvedTrack("Other Song", "Artist", False)
    result = filter_prefer_explicit([clean, explicit, other])
    assert set(result) == {explicit, other}


def test_prefer_explicit_matches_title_and_artist_loosely():
    # Same song, different casing/whitespace -- still recognized as a dupe.
    clean = FakeResolvedTrack("  song  ", "ARTIST", False)
    explicit = FakeResolvedTrack("Song", "artist", True)
    assert filter_prefer_explicit([clean, explicit]) == [explicit]
