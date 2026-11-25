"""Deciding whether a search result is the track wanted (from Music-Sync's tests)."""

import pytest

from streamrip.client.audio_match import best_match, score
from streamrip.sync.models import Track


def t(title: str, *artists: str, duration_ms: int | None = 200_000, album: str = ""):
    return Track(title, list(artists or ["Artist"]), album, duration_ms)


# How one service writes a track, and how another writes the same recording.
@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        (
            t("Travesuras", "Nicky Jam x J Balvin"),
            t("Travesuras", "Nicky Jam", "J Balvin", duration_ms=206_000),
        ),
        (
            t("Thrift Shop", "Macklemore & Ryan Lewis"),
            t("Thrift Shop (feat. Wanz)", "Macklemore", "Ryan Lewis"),
        ),
        (
            t("Don\u2019t Stop Me Now", "Queen"),
            t("Don't Stop Me Now (Remastered 2011)", "Queen"),
        ),
        (t("Alone, Pt. II", "Alan Walker"), t("Alone, Part II", "Alan Walker")),
        (
            t("Hotel California \u2013 2013 Remaster", "Eagles"),
            t("Hotel California (2013 Remaster)", "Eagles"),
        ),
        (t("TiK ToK", "Kesha"), t("TiK ToK", "Ke$ha")),
        (t("Song", "The Beatles"), t("Song", "Beatles")),
        (
            t("Stay (with Justin Bieber)", "The Kid LAROI"),
            t("Stay", "The Kid LAROI", "Justin Bieber"),
        ),
        (
            t("Refugee", "Tom Petty and the Heartbreakers"),
            t("Refugee", "Tom Petty"),
        ),
        (t("Live Forever", "Oasis"), t("Live Forever - Remastered", "Oasis")),
    ],
)
def test_the_same_recording_written_differently_matches(wanted, found):
    assert score(wanted, found) >= 0.9


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        (t("Love Story (Taylor\u2019s Version)"), t("Love Story")),
        (t("Snowfall"), t("Snowfall (Sped Up)")),
        (t("Lose Yourself"), t("Lose Yourself (Instrumental)")),
        (t("Song"), t("Song - Live at Wembley")),
        (t("Live Wire"), t("Live Wire (Live)")),
        (t("(Untitled)"), t("[Intro]")),  # both used to shrink to ""
    ],
)
def test_another_recording_does_not_match(wanted, found):
    assert score(wanted, found) < 0.8


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        (t("Song", "Artist"), t("Song", "Somebody Else")),
        (t("Jane", "Radio Blazers"), t("Jane", "Roy Blair")),  # alike, not the band
    ],
)
def test_a_different_artist_never_matches(wanted, found):
    assert score(wanted, found) == 0.0


def test_length_counts_only_when_known():
    assert score(t("Song", duration_ms=400_000), t("Song")) < score(
        t("Song"), t("Song")
    )
    assert score(t("Song", duration_ms=None), t("Song")) >= 0.95


@pytest.mark.parametrize(
    ("wanted", "found", "same"),
    [
        ("Nightcall - Part 1", "Nightcall - Part 2", False),
        ("Symphony No. 5", "Symphony No. 7", False),
        ("Alone, Pt. I", "Alone, Pt. II", False),
        ("Alone, Pt. II", "Alone, Part 2", True),
        ("Nightcall, Pt. One", "Nightcall (Part 1)", True),
        ("Symphony No. 5: I. Allegro", "Symphony No. 5: II. Andante", False),
        ("Summer of '69", "Summer Of 69 - Remastered 2008", True),
        ("I. Robot", "I Robot", True),
    ],
)
def test_titles_with_other_numbers_are_other_songs(wanted, found, same):
    assert (score(t(wanted), t(found)) >= 0.8) is same


def test_without_artists_the_title_and_length_decide():
    # A CSV with titles only used to be refused, as the missing artist counted as a mismatch.
    csv_row = Track("Get Lucky", [], duration_ms=None)
    assert score(csv_row, t("Get Lucky", "Daft Punk")) >= 0.9
    assert score(csv_row, t("Something Else", "Daft Punk")) < 0.8
    # A result without artist data is no match for a track that has one: a video
    # whose channel says nothing would let another artist's "Get Lucky" in.
    assert score(t("Get Lucky", "Daft Punk"), Track("Get Lucky", [])) == 0


def test_best_match_picks_the_highest_score_and_respects_the_threshold():
    match = best_match(t("Song"), [t("Something Else"), t("Song (Live)"), t("Song")])
    assert match is not None
    assert match[0].title == "Song"
    assert best_match(t("Song"), [t("Something Else")]) is None


def test_ties_go_to_the_closest_version_and_then_to_the_same_album():
    wanted = t("Summertime Sadness (Cedric Gervais Remix)", "Lana Del Rey")
    kaskade = t("Summertime Sadness (Kaskade Remix)", "Lana Del Rey")
    cedric = t(wanted.title, "Lana Del Rey")
    assert best_match(wanted, [kaskade, cedric]) == (cedric, 1.0)

    wanted = t("Wonderwall", "Oasis", album="(What's The Story) Morning Glory?")
    hits = t("Wonderwall", "Oasis", album="Time Flies")
    original = t("Wonderwall", "Oasis", album=wanted.album)
    assert best_match(wanted, [hits, original]) == (original, 1.0)
