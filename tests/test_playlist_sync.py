"""Finding tracks on another service and adding them: the engine of `streamrip playlist`."""

import pytest
from playlist_support import ISRC_A, FakeProvider, track

from streamrip.sync.engine import (
    import_albums,
    import_tracks,
    resolve_tracks,
    select_tracks,
)
from streamrip.sync.errors import ApiError, ProviderError, QuotaExceededError
from streamrip.sync.models import PlaylistInfo, Track
from streamrip.sync.providers.base import search_queries

CATALOG = [
    track("Song A", "Artist A", ids={"fake": "1"}, isrc=ISRC_A),
    track("Song B", "Artist B", ids={"fake": "2"}, duration_ms=180_000),
]


def source_tracks() -> list[Track]:
    return [
        Track("Song A", ["Artist A"], isrc=ISRC_A),  # found by ISRC
        Track("Song B (feat. X)", ["Artist B"], duration_ms=181_000),  # by a search
        Track("Nonexistent", ["Nobody"]),  # not found
        Track("Song A", ["Artist A"], isrc=ISRC_A),  # repeated
    ]


def test_import_creates_the_playlist_and_reports_everything():
    service = FakeProvider(catalog=CATALOG)
    result = import_tracks(service, source_tracks(), "Mix")
    assert [m.method for m in result.matched] == ["isrc", "search"]
    assert [str(miss.track) for miss in result.misses] == ["Nobody - Nonexistent"]
    assert (result.duplicates, result.added, result.already_there) == (1, 2, 0)
    (_, playlist), *_ = service.playlists_by_id.values()
    assert [service.native_id(t) for t in playlist] == ["1", "2"]


def test_running_it_again_adds_and_looks_up_nothing():
    service = FakeProvider(catalog=CATALOG)
    findable = source_tracks()[:2]
    import_tracks(service, findable, "Mix")
    asked = (list(service.searches), list(service.isrc_lookups))

    again = import_tracks(service, findable, "mix")  # the name match ignores case
    assert (again.added, again.already_there) == (0, 2)
    assert [m.method for m in again.matched] == ["playlist", "playlist"]
    assert (service.searches, service.isrc_lookups) == asked
    assert (service.add_calls, len(service.playlists_by_id)) == (1, 1)


@pytest.mark.parametrize(
    "wanted",
    [
        Track("Whatever", ["Someone"], ids={"fake": "1"}),  # its id on the target
        Track("Whatever", ["Someone"], isrc=ISRC_A),  # its ISRC
        Track("Song A (feat. X) - Remastered", ["Artist A"], duration_ms=200_000),
    ],
    ids=["id", "isrc", "title and artist"],
)
def test_a_track_in_the_playlist_is_recognised(wanted):
    service = FakeProvider()  # an empty catalogue: a lookup would find nothing
    (match,), misses = resolve_tracks(service, [wanted], present=[CATALOG[0]])
    assert misses == []
    assert (match.method, match.track) == ("playlist", CATALOG[0])


def test_tracks_can_go_to_the_favourites_skipping_what_is_already_there():
    service = FakeProvider(catalog=CATALOG, liked=[CATALOG[0]])
    result = import_tracks(service, source_tracks(), None)
    assert (result.added, result.already_there) == (1, 1)
    assert service.playlists_by_id == {}
    assert [service.native_id(t) for t in service.liked] == ["1", "2"]


def test_albums_go_to_the_favourites():
    service = FakeProvider()
    service.albums = [
        track("Discovery", "Daft Punk", ids={"fake": "a1"}),
        track("Homework", "Daft Punk", ids={"fake": "a2"}),
    ]
    rows = [
        Track("Discovery", ["Daft Punk"]),  # artist,album: the album is the title
        Track("One More Time", ["Daft Punk"], album="Discovery"),  # a track list
        Track("Alive 2007", ["Daft Punk"]),  # not there
    ]
    result = import_albums(service, rows)
    assert [a.title for a in service.favorite_albums] == ["Discovery"]
    assert (result.added, result.duplicates) == (1, 1)
    assert [m.reason for m in result.misses] == ["not on Fake"]


def test_a_dry_run_changes_nothing():
    service = FakeProvider(catalog=CATALOG)
    result = import_tracks(service, source_tracks(), "Mix", dry_run=True)
    assert (result.added, result.dry_run) == (2, True)
    assert service.playlists_by_id == {}


def test_a_playlist_that_is_not_yours_is_refused():
    service = FakeProvider(catalog=CATALOG, readable=False)
    service.create_playlist("Mix")
    with pytest.raises(ProviderError, match="not yours"):
        import_tracks(service, source_tracks(), "Mix")


def test_your_own_playlist_goes_before_one_you_follow_by_that_name():
    class Following(FakeProvider):
        def playlists(self):
            return [
                PlaylistInfo("theirs", "Mix", 1, readable=False),
                *super().playlists(),
            ]

    service = Following(catalog=CATALOG)
    own = service.existing_playlist("Mix")
    assert import_tracks(service, source_tracks(), "mix").playlist_id == own


def test_imports_into_several_playlists_list_them_once():
    class Counting(FakeProvider):
        listed = 0

        def playlists(self):
            self.listed += 1
            return super().playlists()

    service = Counting(catalog=CATALOG)
    known = service.playlists()
    import_tracks(service, source_tracks(), "Mix", playlists=known)
    again = import_tracks(service, source_tracks(), "Mix", playlists=known)
    assert (service.listed, again.added, len(service.playlists_by_id)) == (1, 0, 1)


def test_one_track_that_cannot_be_looked_up_does_not_stop_the_run():
    class Flaky(FakeProvider):
        def search(self, query):
            if "Broken" in query:
                raise ApiError(400, "bad query")
            return super().search(query)

    wanted = [
        Track("Broken", ["X"]),
        Track("Song B", ["Artist B"], duration_ms=180_000),
    ]
    matches, misses = resolve_tracks(Flaky(catalog=CATALOG), wanted)
    assert len(matches) == 1
    assert [miss.track.title for miss in misses] == ["Broken"]


@pytest.mark.parametrize(
    "error", [QuotaExceededError(429, "quota"), ApiError(500, "boom")]
)
def test_quota_and_server_errors_stop_the_run(error):
    class Broken(FakeProvider):
        def search(self, query):
            raise error

    with pytest.raises(ApiError):
        resolve_tracks(Broken(catalog=CATALOG), [Track("Song B", ["Artist B"])])


@pytest.mark.parametrize(
    ("wanted", "reason", "closest"),
    [
        (Track("Nothing Like It", ["Nobody"]), "not on Fake", None),
        # Same artist, another song: that candidate says nothing, so it is not shown.
        (
            Track("Hallo, Lieve Mensen", ["Artist B"], duration_ms=180_000),
            "only other songs on Fake",
            None,
        ),
        (
            Track("Song B (Live)", ["Artist B"], duration_ms=180_000),
            "only another version",
            "Song B",
        ),
        # The same song, but twelve seconds longer: not sure enough.
        (
            Track("Song B", ["Artist B"], duration_ms=192_000),
            "score too low for a match (0.90, needs 0.95)",
            "Song B",
        ),
    ],
)
def test_each_track_not_found_says_why(wanted, reason, closest):
    class Everything(FakeProvider):
        def search(self, query):  # every track in the catalogue, for every search
            return self.catalog

    _, (miss,) = resolve_tracks(Everything(catalog=CATALOG), [wanted], min_score=0.95)
    assert miss.reason == reason
    assert (miss.closest.track.title if miss.closest else None) == closest


def test_the_searches_go_from_specific_to_loose_without_repeats():
    assert search_queries(Track("Jóga - Remastered", ["Björk"])) == [
        "Jóga - Remastered Björk",
        "Jóga Björk",
        "joga bjork",
        "Jóga",
    ]


class Scripted(FakeProvider):
    """Answers each search from a script, and remembers what was asked."""

    def __init__(self, answers: dict[str, list[Track] | Exception]):
        super().__init__()
        self.answers = answers
        self.asked: list[str] = []

    def search(self, query):
        self.asked.append(query)
        answer = self.answers.get(query, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_search_the_service_refuses_does_not_stop_the_other_searches():
    wanted = Track("Song (feat. X)", ["Artist"], duration_ms=200_000)
    right = track("Song", ids={"fake": "1"})
    service = Scripted(
        {"Song (feat. X) Artist": ApiError(400, "bad query"), "Song Artist": [right]}
    )
    match = service.find(wanted)
    assert match is not None
    assert match.track is right


def test_the_best_hit_of_all_searches_wins_and_a_convincing_one_ends_the_search():
    wanted = Track("Song - Remastered", ["Artist"], duration_ms=200_000)
    live, studio = (
        track("Song (Live)", ids={"fake": "1"}),
        track("Song", ids={"fake": "2"}),
    )
    service = Scripted({"Song - Remastered Artist": [live], "Song Artist": [studio]})
    match = service.find(wanted)
    assert match is not None
    assert (match.track, match.score) == (studio, 1.0)
    assert service.asked == [
        "Song - Remastered Artist",
        "Song Artist",
    ]  # no looser ones


def test_a_track_the_searches_miss_is_found_on_its_album():
    wanted = Track(
        "One More Time (Radio Edit)",
        ["Daft Punk"],
        album="Discovery",
        duration_ms=320_000,
    )
    on_album = track(
        "One More Time (Radio Edit)",
        "Daft Punk",
        ids={"fake": "t1"},
        duration_ms=320_000,
    )
    service = Scripted({})  # no search finds it
    service.albums = [track("Discovery", "Daft Punk", ids={"fake": "a1"})]
    service.album_contents = {
        "a1": [track("Aerodynamic", "Daft Punk", ids={"fake": "t2"}), on_album]
    }
    match = service.find(wanted)
    assert match is not None
    assert match.track is on_album


def library() -> FakeProvider:
    service = FakeProvider(liked=[track("Liked", ids={"fake": "9"})])
    service.existing_playlist("Road trip", track("In list", ids={"fake": "8"}))
    return service


def test_one_playlist_is_chosen_by_name_or_id():
    assert select_tracks(library(), playlist="road TRIP")[0][0] == "Road trip"
    assert select_tracks(library(), playlist="pl1")[0][0] == "Road trip"


def test_everything_skips_what_is_unreadable_or_empty_with_a_note():
    service, notes = library(), []
    service.existing_playlist("Empty")
    chosen = select_tracks(service, on_skip=notes.append)
    assert [name for name, _ in chosen] == ["Liked Songs", "Road trip"]
    assert notes == ["skipping 'Empty': it is empty"]

    service.readable = False
    notes.clear()
    assert [n for n, _ in select_tracks(service, on_skip=notes.append)] == [
        "Liked Songs"
    ]
    assert "Road trip" in notes[0]


def test_a_missing_or_unreadable_playlist_is_an_error():
    service = library()
    with pytest.raises(ProviderError, match="No playlist"):
        select_tracks(service, playlist="nope")
    service.readable = False
    with pytest.raises(ProviderError, match="not yours"):
        select_tracks(service, playlist="Road trip")


def test_asking_for_an_empty_one_is_an_error():
    service = FakeProvider()
    service.existing_playlist("Empty")
    with pytest.raises(ProviderError, match='"Empty" is empty'):
        select_tracks(service, playlist="Empty")
    with pytest.raises(ProviderError, match='"Liked Songs" is empty'):
        select_tracks(service, liked=True)
