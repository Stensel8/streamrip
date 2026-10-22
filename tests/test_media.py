import pytest

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
        info = TrackInfo(id="1", explicit=explicit)
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


@pytest.mark.parametrize(
    ("clean", "explicit"),
    [
        (("Song", "Artist"), ("Song", "Artist")),
        # Same song, different casing/whitespace -- still recognized as a dupe.
        (("  song  ", "ARTIST"), ("Song", "artist")),
    ],
)
def test_prefer_explicit_drops_the_clean_duplicate(clean, explicit):
    clean_track = FakeResolvedTrack(*clean, False)
    explicit_track = FakeResolvedTrack(*explicit, True)
    assert filter_prefer_explicit([clean_track, explicit_track]) == [explicit_track]


@pytest.mark.parametrize("explicit", [False, True])
def test_prefer_explicit_keeps_a_track_that_has_no_duplicate(explicit):
    track = FakeResolvedTrack("Song", "Artist", explicit)
    assert filter_prefer_explicit([track]) == [track]


def test_prefer_explicit_leaves_unrelated_tracks_alone():
    clean = FakeResolvedTrack("Song", "Artist", False)
    explicit = FakeResolvedTrack("Song", "Artist", True)
    other = FakeResolvedTrack("Other Song", "Artist", False)
    result = filter_prefer_explicit([clean, explicit, other])
    assert set(result) == {explicit, other}
