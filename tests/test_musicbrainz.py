import time

import pytest
import responses
from playlist_support import FakeProvider

from streamrip import __version__
from streamrip.sync.engine import resolve_tracks
from streamrip.sync.models import Track
from streamrip.sync.musicbrainz import API, MusicBrainz

ISRC = "GBDUW0000059"
SIBLING = "GBDUW0600059"


def recording(title="Harder, Better, Faster, Stronger", isrcs=(ISRC, SIBLING), **more):
    return {
        "id": "rec-1",
        "title": title,
        "length": 224293,
        "artist-credit": [{"name": "Daft Punk"}],
        "isrcs": list(isrcs),
        **more,
    }


@responses.activate
def test_another_isrc_of_the_same_recording_is_found_by_isrc():
    responses.get(
        f"{API}/isrc/{ISRC}", json={"isrc": ISRC, "recordings": [recording()]}
    )
    found = MusicBrainz(interval=0).isrcs_for(Track("HBFS", ["Daft Punk"], isrc=ISRC))
    assert found == ([SIBLING], 0.95)
    sent = responses.calls[0].request
    assert (
        sent.headers["User-Agent"]
        == f"streamrip/{__version__} (https://opencommit.eu/Stensel8/streamrip)"
    )
    assert "fmt=json" in sent.url


@responses.activate
def test_a_track_without_isrc_is_found_by_title_and_artist():
    responses.get(
        f"{API}/recording",
        json={"recordings": [recording(title="Other song"), recording()]},
    )
    responses.get(f"{API}/recording/rec-1", json=recording())
    wanted = Track(
        "Harder, Better, Faster, Stronger", ["Daft Punk"], duration_ms=224000
    )
    isrcs, sure = MusicBrainz(interval=0).isrcs_for(wanted)
    assert isrcs == [ISRC, SIBLING]
    assert sure >= 0.9
    assert (
        'recording:"Harder, Better, Faster, Stronger" AND artist:"Daft Punk"'
        in responses.calls[0].request.params["query"]
    )


@responses.activate
def test_a_recording_that_does_not_match_well_enough_gives_nothing():
    responses.get(
        f"{API}/recording",
        json={"recordings": [recording(title="Something else entirely")]},
    )
    assert MusicBrainz(interval=0).isrcs_for(Track("Wanted", ["Daft Punk"])) == (
        [],
        0.0,
    )
    assert len(responses.calls) == 1  # no lookup of a recording nobody believes in


@responses.activate
def test_a_busy_musicbrainz_is_asked_once_more_and_never_fails_the_run():
    responses.get(f"{API}/isrc/{ISRC}", status=503)
    responses.get(f"{API}/isrc/{ISRC}", json={"recordings": [recording()]})
    mb = MusicBrainz(interval=0, pause=0)
    assert mb.isrcs_for(Track("HBFS", ["Daft Punk"], isrc=ISRC)) == ([SIBLING], 0.95)
    assert len(responses.calls) == 2


@responses.activate
def test_a_failure_is_not_remembered_but_an_answer_is():
    responses.get(f"{API}/isrc/{ISRC}", status=503)
    mb = MusicBrainz(interval=0, pause=0)
    track = Track("HBFS", ["Daft Punk"], isrc=ISRC)
    assert mb.isrcs_for(track) == ([], 0.95)  # busy twice: nothing, no exception
    assert len(responses.calls) == 2
    responses.replace(
        responses.GET, f"{API}/isrc/{ISRC}", json={"recordings": [recording()]}
    )
    assert mb.isrcs_for(track) == ([SIBLING], 0.95)  # asked again
    assert mb.isrcs_for(track) == ([SIBLING], 0.95)
    assert len(responses.calls) == 3  # and now kept


@responses.activate
def test_requests_keep_their_distance():
    responses.get(f"{API}/isrc/{ISRC}", json={"recordings": []})
    responses.get(f"{API}/isrc/{SIBLING}", json={"recordings": []})
    mb = MusicBrainz(interval=0.2)
    started = time.monotonic()
    mb.isrcs_for(Track("a", ["b"], isrc=ISRC))
    mb.isrcs_for(Track("a", ["b"], isrc=SIBLING))
    assert time.monotonic() - started >= 0.2


class Knows:
    """A MusicBrainz that says a recording has another ISRC."""

    def __init__(self, isrcs):
        self.isrcs = isrcs

    def isrcs_for(self, _track):
        return self.isrcs, 0.95


@pytest.fixture
def service():
    # Listed there under the other ISRC and a title no search will find.
    return FakeProvider(
        "tidal", [Track("Completely Different Name", ["Nobody"], isrc=SIBLING)]
    )


def test_a_track_the_service_lacks_is_found_under_another_isrc_of_its_recording(
    service,
):
    wanted = Track("Harder, Better, Faster, Stronger", ["Daft Punk"], isrc=ISRC)
    matches, misses = resolve_tracks(service, [wanted], musicbrainz=Knows([SIBLING]))
    assert not misses
    assert (matches[0].method, matches[0].track.isrc, matches[0].wanted) == (
        "musicbrainz",
        SIBLING,
        wanted,
    )


def test_without_musicbrainz_that_track_stays_missing(service):
    wanted = Track("Harder, Better, Faster, Stronger", ["Daft Punk"], isrc=ISRC)
    matches, misses = resolve_tracks(service, [wanted])
    assert not matches
    assert len(misses) == 1


def test_musicbrainz_has_nothing_to_say_then_it_stays_missing(service):
    wanted = Track("Harder, Better, Faster, Stronger", ["Daft Punk"], isrc=ISRC)
    _, misses = resolve_tracks(service, [wanted], musicbrainz=Knows([]))
    assert len(misses) == 1


@responses.activate
def test_a_live_recording_is_not_taken_for_the_studio_one_and_one_without_isrcs_is_skipped():
    live = recording(id="live", disambiguation="live, 2006-04-29: Coachella")
    bare = {**recording(), "id": "bare"}
    good = {**recording(), "id": "good"}
    responses.get(f"{API}/recording", json={"recordings": [live, bare, good]})
    responses.get(f"{API}/recording/bare", json={**bare, "isrcs": []})
    responses.get(f"{API}/recording/good", json=good)
    wanted = Track(
        "Harder, Better, Faster, Stronger", ["Daft Punk"], duration_ms=224000
    )
    isrcs, _ = MusicBrainz(interval=0).isrcs_for(wanted)
    assert isrcs == [ISRC, SIBLING]
    assert [
        c.request.url.split("/ws/2/")[1].split("?")[0] for c in responses.calls[1:]
    ] == ["recording/bare", "recording/good"]


def test_a_second_opinion_that_is_less_sure_than_the_score_asked_is_not_taken(service):
    wanted = Track("Harder, Better, Faster, Stronger", ["Daft Punk"], isrc=ISRC)
    _, misses = resolve_tracks(
        service, [wanted], min_score=0.99, musicbrainz=Knows([SIBLING])
    )
    assert len(misses) == 1  # MusicBrainz is 0.95 sure
