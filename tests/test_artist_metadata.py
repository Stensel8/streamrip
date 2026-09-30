"""An artist's discography is the artist's own releases, each listed once."""

from streamrip.metadata import ArtistMetadata

ME = 43470


def _qobuz(*albums):
    return {"id": ME, "name": "30 Seconds To Mars", "albums": {"items": list(albums)}}


def _album(id, title, artists=(ME,), n=12, version=None, explicit=False, bits=16):
    return {
        "id": id,
        "title": title,
        "version": version,
        "tracks_count": n,
        "parental_warning": explicit,
        "maximum_bit_depth": bits,
        "maximum_sampling_rate": 44.1,
        "artist": {"id": artists[0]},
        "artists": [{"id": a} for a in artists],
    }


def _ids(resp, source="qobuz", prefer_explicit=True):
    return ArtistMetadata.from_resp(resp, source, prefer_explicit).album_ids()


def test_qobuz_leaves_out_other_artists_covers_but_keeps_collaborations():
    resp = _qobuz(
        _album("own", "The Kill", n=2),
        _album("cover1", "The Kill", artists=(111,), n=1),
        _album("cover2", "The Kill", artists=(222,), n=1),
        _album("collab", "Wouldn't Change A Thing", artists=(333, ME), n=1),
    )
    assert _ids(resp) == ["own", "collab"]


def test_qobuz_keeps_the_explicit_copy_of_a_clean_and_explicit_listing():
    resp = _qobuz(
        _album("clean", "LOVE LUST FAITH + DREAMS", explicit=False, bits=24),
        _album("explicit", "LOVE LUST FAITH + DREAMS", explicit=True, bits=24),
    )
    assert _ids(resp) == ["explicit"]


def test_qobuz_keeps_the_higher_quality_copy_of_the_same_release():
    resp = _qobuz(
        _album("cd", "The Kill (Bury Me)", n=1, bits=16),
        _album("hires", "The Kill (Bury Me)", n=1, bits=24),
    )
    assert _ids(resp) == ["hires"]


def test_qobuz_editions_and_track_counts_keep_releases_apart():
    resp = _qobuz(
        _album("std", "This Is War", n=13),
        _album("deluxe", "This Is War", n=15, version="Deluxe"),
        _album("single", "This Is War", n=2),
        _album("remix", "Seasons", n=2, version="JAYEM Remix"),
        _album("acoustic", "Seasons", n=2, version="Acoustic"),
    )
    assert _ids(resp) == ["std", "deluxe", "single", "remix", "acoustic"]


def test_qobuz_without_prefer_explicit_keeps_duplicates_but_not_covers():
    resp = _qobuz(
        _album("clean", "LOVE LUST FAITH + DREAMS", explicit=False),
        _album("explicit", "LOVE LUST FAITH + DREAMS", explicit=True),
        _album("cover", "The Kill", artists=(111,), n=1),
    )
    assert _ids(resp, prefer_explicit=False) == ["clean", "explicit"]


def test_qobuz_album_without_artist_credits_is_kept():
    album = _album("x", "Unknown Credits")
    del album["artist"], album["artists"]
    assert _ids(_qobuz(album)) == ["x"]


def test_deezer_albums_are_left_as_listed():
    resp = {"name": "A", "albums": [{"id": 1, "title": "X"}, {"id": 2, "title": "X"}]}
    assert _ids(resp, source="deezer") == [1, 2]


def test_tidal_versions_stay_distinct_and_matching_versions_are_deduplicated():
    def album(id, version, explicit=False):
        return {
            "id": id,
            "title": "Seasons",
            "version": version,
            "artists": [{"id": ME}],
            "numberOfTracks": 2,
            "explicit": explicit,
            "audioQuality": "LOSSLESS",
        }

    resp = {
        "name": "30 Seconds To Mars",
        "albums": [
            album("standard", None),
            album("remix", "Remix"),
            album("acoustic-clean", " [Acoustic] "),
            album("acoustic-explicit", "(acoustic)", explicit=True),
        ],
    }
    assert _ids(resp, source="tidal") == ["standard", "remix", "acoustic-explicit"]
