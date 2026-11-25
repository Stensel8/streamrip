import time
from types import SimpleNamespace

import pytest
import responses
from playlist_support import FakeProvider

from streamrip.sync.engine import resolve_tracks
from streamrip.sync.models import Track
from streamrip.sync.musicbrainz import API, MusicBrainz

ISRC = "GBDUW0000059"
SIBLING = "GBDUW0600059"
TITLE = "Harder, Better, Faster, Stronger"


def recording(title=TITLE, isrcs=(ISRC, SIBLING), **more):
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
    # MusicBrainz wants to know who is asking.
    assert responses.calls[0].request.headers["User-Agent"].startswith("streamrip/")


@responses.activate
def test_a_track_without_isrc_is_found_by_title_and_artist():
    responses.get(
        f"{API}/recording",
        json={"recordings": [recording(title="Other song"), recording()]},
    )
    responses.get(f"{API}/recording/rec-1", json=recording())
    wanted = Track(TITLE, ["Daft Punk"], duration_ms=224000)
    isrcs, sure = MusicBrainz(interval=0).isrcs_for(wanted)
    assert isrcs == [ISRC, SIBLING]
    assert sure >= 0.9


@responses.activate
def test_a_recording_that_does_not_match_well_enough_gives_nothing():
    other = recording(title="Something else entirely")
    responses.get(f"{API}/recording", json={"recordings": [other]})
    wanted = Track("Wanted", ["Daft Punk"])
    assert MusicBrainz(interval=0).isrcs_for(wanted) == ([], 0.0)


@pytest.mark.parametrize("title", [TITLE, "Alive"])  # no live version, just the word
@responses.activate
def test_a_live_recording_is_not_taken_for_the_studio_one(title):
    live = recording(title, id="live", disambiguation="live, 2006-04-29: Coachella")
    responses.get(f"{API}/recording", json={"recordings": [live, recording(title)]})
    responses.get(f"{API}/recording/live", json={**live, "isrcs": ["GBXXX0000001"]})
    responses.get(f"{API}/recording/rec-1", json=recording(title))
    wanted = Track(title, ["Daft Punk"], duration_ms=224000)
    isrcs, _ = MusicBrainz(interval=0).isrcs_for(wanted)
    assert isrcs == [ISRC, SIBLING]


@responses.activate
def test_a_busy_musicbrainz_is_asked_once_more():
    responses.get(f"{API}/isrc/{ISRC}", status=503)
    responses.get(f"{API}/isrc/{ISRC}", json={"recordings": [recording()]})
    mb = MusicBrainz(interval=0, pause=0)
    assert mb.isrcs_for(Track("HBFS", ["Daft Punk"], isrc=ISRC)) == ([SIBLING], 0.95)


@responses.activate
def test_a_failure_is_not_remembered_but_an_answer_is():
    responses.get(f"{API}/isrc/{ISRC}", status=503)
    mb = MusicBrainz(interval=0, pause=0)
    wanted = Track("HBFS", ["Daft Punk"], isrc=ISRC)
    assert mb.isrcs_for(wanted) == ([], 0.95)  # busy twice: nothing, no exception
    responses.replace(
        responses.GET, f"{API}/isrc/{ISRC}", json={"recordings": [recording()]}
    )
    assert mb.isrcs_for(wanted) == ([SIBLING], 0.95)
    assert mb.isrcs_for(wanted) == ([SIBLING], 0.95)
    assert len(responses.calls) == 3  # the answer is kept


@responses.activate
def test_requests_keep_their_distance():
    responses.get(f"{API}/isrc/{ISRC}", json={"recordings": []})
    responses.get(f"{API}/isrc/{SIBLING}", json={"recordings": []})
    mb = MusicBrainz(interval=0.1)
    started = time.monotonic()
    mb.isrcs_for(Track("a", ["b"], isrc=ISRC))
    mb.isrcs_for(Track("a", ["b"], isrc=SIBLING))
    assert time.monotonic() - started >= 0.1


@pytest.fixture
def service():
    # Listed there under the other ISRC and a title no search will find.
    return FakeProvider(
        "tidal", [Track("Completely Different Name", ["Nobody"], isrc=SIBLING)]
    )


def knows(*isrcs: str) -> SimpleNamespace:
    return SimpleNamespace(isrcs_for=lambda _track: (list(isrcs), 0.95))


def test_a_track_the_service_lacks_is_found_under_another_isrc_of_its_recording(
    service,
):
    wanted = Track(TITLE, ["Daft Punk"], isrc=ISRC)
    matches, misses = resolve_tracks(service, [wanted], musicbrainz=knows(SIBLING))
    assert not misses
    assert (matches[0].method, matches[0].track.isrc) == ("musicbrainz", SIBLING)


def test_when_musicbrainz_knows_nothing_the_track_stays_missing(service):
    wanted = Track(TITLE, ["Daft Punk"], isrc=ISRC)
    _, misses = resolve_tracks(service, [wanted], musicbrainz=knows())
    assert len(misses) == 1
    # Nor when the service has only a live version under the other ISRC.
    live = Track(f"{TITLE} (Live)", ["Daft Punk"], isrc=SIBLING)
    _, misses = resolve_tracks(
        FakeProvider("tidal", [live]), [wanted], musicbrainz=knows(SIBLING)
    )
    assert len(misses) == 1
