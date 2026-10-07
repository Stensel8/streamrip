"""Scoring a YouTube Music result against a Spotify track.

The score cases are those of Music-Sync's tests/test_matching.py
(https://github.com/Stensel8/Music-Sync), whose matching this is.
"""

import pytest

from streamrip.client.audio_match import MatchTrack, normalize, score, simplify_title


def t(
    title: str,
    artists: tuple[str, ...] = ("Artist",),
    duration_ms: int | None = 200_000,
):
    return MatchTrack(title, list(artists), duration_ms=duration_ms)


def a(title: str, *artists: str, duration_ms: int | None = 200_000, album: str = ""):
    return MatchTrack(title, list(artists), album, duration_ms)


@pytest.mark.parametrize(
    ("title", "simplified", "normalized"),
    [
        ("Song (feat. Someone) - Remastered 2011", "Song", "song"),
        ("S\u00f3ng!", "S\u00f3ng!", "song"),
        ("Song - 2011 Remaster", "Song", "song"),
        ("Song [Deluxe Edition]", "Song", "song"),
    ],
)
def test_titles_lose_their_noise(title, simplified, normalized):
    assert simplify_title(title) == simplified
    assert normalize(title) == normalized


def test_identical_tracks_score_one():
    assert score(t("Song"), t("Song")) == 1.0


@pytest.mark.parametrize(
    "candidate", ["Song (Remastered 2011)", "Song - 2011 Remaster", "Song (feat. X)"]
)
def test_remaster_and_featuring_do_not_matter(candidate):
    assert score(t("Song"), t(candidate)) >= 0.95


@pytest.mark.parametrize(
    "candidate", ["Song - Live at Wembley", "Song (Acoustic)", "Song (Remix)"]
)
def test_another_version_is_not_the_same_track(candidate):
    assert score(t("Song"), t(candidate)) < 0.8


def test_a_different_artist_never_matches():
    assert score(t("Song", ("Artist",)), t("Song", ("Somebody Else",))) == 0.0


@pytest.mark.parametrize(
    ("wanted", "found"), [("The Beatles", "Beatles"), ("Bj\u00f6rk", "Bjork")]
)
def test_artist_spelling_is_forgiving(wanted, found):
    assert score(t("Song", (wanted,)), t("Song", (found,))) >= 0.8


def test_a_very_different_length_lowers_the_score():
    assert score(t("Song", duration_ms=400_000), t("Song")) < score(
        t("Song"), t("Song")
    )


def test_an_unknown_duration_is_neutral():
    assert score(t("Song", duration_ms=None), t("Song")) >= 0.95


# How one service writes a track, and how the other writes the same recording.
@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        (
            a("Travesuras", "Nicky Jam x J Balvin"),
            a("Travesuras", "Nicky Jam", "J Balvin", duration_ms=206_000),
        ),
        (
            a("Thrift Shop", "Macklemore & Ryan Lewis"),
            a("Thrift Shop (feat. Wanz)", "Macklemore", "Ryan Lewis"),
        ),
        (
            a("Don\u2019t Stop Me Now", "Queen"),
            a("Don't Stop Me Now (Remastered 2011)", "Queen"),
        ),
        (a("Rock 'n' Roll Star", "Oasis"), a("Rock and Roll Star", "Oasis")),
        (a("Alone, Pt. II", "Alan Walker"), a("Alone, Part II", "Alan Walker")),
        (a("TiK ToK", "Kesha"), a("TiK ToK", "Ke$ha")),
        (a("Thunderstruck", "AC/DC"), a("Thunderstruck", "ACDC")),
        (a("Levels", "Avicii"), a("Levels (Original Mix)", "Avicii")),
        (
            a("Stay (with Justin Bieber)", "The Kid LAROI"),
            a("Stay", "The Kid LAROI", "Justin Bieber"),
        ),
    ],
)
def test_the_same_recording_written_differently_matches(wanted, found):
    assert score(wanted, found) >= 0.9


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        (
            a("Love Story (Taylor\u2019s Version)", "Taylor Swift"),
            a("Love Story", "Taylor Swift"),
        ),
        (a("Snowfall", "\u00d8neheart"), a("Snowfall (Sped Up)", "\u00d8neheart")),
        (a("Lose Yourself", "Eminem"), a("Lose Yourself (Instrumental)", "Eminem")),
        (
            a("Someone Like You", "Adele"),
            a("Someone Like You (Karaoke Version)", "Adele"),
        ),
    ],
)
def test_another_recording_does_not_match(wanted, found):
    assert score(wanted, found) < 0.8


@pytest.mark.parametrize(
    ("wanted", "found", "same"),
    [
        ("Song 55", "Song 5", False),
        ("Nightcall - Part 1", "Nightcall - Part 2", False),
        ("Alone, Pt. II", "Alone, Part 2", True),
        ("Song (Live 1986)", "Song (Live)", True),
        ("Summer of '69", "Summer Of 69 - Remastered 2008", True),
    ],
)
def test_titles_with_other_numbers_are_other_songs(wanted, found, same):
    assert (score(t(wanted), t(found)) >= 0.8) is same


def test_artists_that_only_share_some_letters_are_someone_else():
    assert score(a("Jane", "Radio Blazers"), a("Jane", "Roy Blair")) == 0.0
    assert (
        score(
            a("Refugee", "Tom Petty and the Heartbreakers"), a("Refugee", "Tom Petty")
        )
        == 1.0
    )
