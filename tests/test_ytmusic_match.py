"""Picking the YouTube Music result that is a Spotify track."""

import pytest

from streamrip.client.audio_match import MatchTrack
from streamrip.client.ytmusic import (
    AudioMatch,
    YouTubeMusicMatcher,
    candidates_from,
    select,
)

WANTED = MatchTrack(
    "Get Lucky (feat. Pharrell Williams)",
    ["Daft Punk", "Pharrell Williams"],
    "Random Access Memories",
    248_000,
)


def song(video_id, title, artists, seconds, album=None, explicit=False):
    return {
        "resultType": "song",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": n} for n in artists],
        "album": {"name": album} if album else None,
        "duration_seconds": seconds,
        "isExplicit": explicit,
    }


def video(video_id, title, channel, seconds):
    return {
        "resultType": "video",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": channel}],
        "duration_seconds": seconds,
    }


def test_a_song_becomes_a_candidate_with_its_album_and_length():
    [found] = candidates_from(
        [song("abc", "Get Lucky", ["Daft Punk"], 248, "Random Access Memories", True)]
    )

    assert found.title == "Get Lucky"
    assert found.artists == ["Daft Punk"]
    assert found.album == "Random Access Memories"
    assert found.duration_ms == 248_000
    assert found.explicit is True
    assert found.is_song is True
    assert found.video_id == "abc"


def test_a_video_title_gives_its_artist_and_loses_its_noise():
    [found] = candidates_from(
        [
            video(
                "v1",
                "Daft Punk - Get Lucky (Official Video) [HD]",
                "DaftPunkVEVO",
                248,
            )
        ]
    )

    assert found.title == "Get Lucky"
    assert found.artists == ["Daft Punk", "DaftPunk"]  # the title's, then the channel's
    assert found.is_song is False


def test_a_version_in_a_video_title_is_kept():
    [found] = candidates_from(
        [video("v1", "Artist - Song (Official Remix)", "Artist - Topic", 200)]
    )

    assert found.title == "Song (Official Remix)"
    assert found.artists == ["Artist", "Artist"]


def test_results_without_an_id_or_title_and_repeats_are_dropped():
    results = [
        {"resultType": "song", "title": "No id", "artists": []},
        {"resultType": "song", "videoId": "x"},
        {"resultType": "artist", "videoId": "y", "title": "An artist"},
        song("same", "Song", ["A"], 100),
        song("same", "Song again", ["A"], 100),
    ]

    assert [c.video_id for c in candidates_from(results)] == ["same"]


def test_the_matching_song_is_chosen_over_other_versions():
    candidates = candidates_from(
        [
            song("live", "Get Lucky - Live", ["Daft Punk"], 250),
            song("right", "Get Lucky", ["Daft Punk", "Pharrell Williams"], 248),
            song("karaoke", "Get Lucky (Karaoke)", ["Backing Band"], 248),
        ]
    )

    match = select(WANTED, candidates)

    assert match is not None
    assert match.track.video_id == "right"
    assert match.score >= 0.9


def test_nothing_is_chosen_when_nothing_matches():
    candidates = candidates_from([song("other", "Another Song", ["Someone Else"], 248)])

    assert select(WANTED, candidates) is None
    assert select(WANTED, []) is None


def test_a_song_of_a_very_different_length_is_another_edit():
    candidates = candidates_from(
        [song("long", "Get Lucky", ["Daft Punk", "Pharrell Williams"], 369)]
    )

    assert select(WANTED, candidates) is None


def test_a_video_has_less_room_in_length_than_a_song():
    # 15 seconds off: fine for a song, too much for a video with an intro.
    as_song = candidates_from([song("s", "Get Lucky", ["Daft Punk"], 263)])
    as_video = candidates_from([video("v", "Daft Punk - Get Lucky", "Daft Punk", 263)])

    assert select(WANTED, as_song) is not None
    assert select(WANTED, as_video) is None


def test_a_song_beats_an_equal_video():
    candidates = candidates_from(
        [
            video("v", "Daft Punk - Get Lucky", "Daft Punk", 248),
            song("s", "Get Lucky", ["Daft Punk"], 248),
        ]
    )

    match = select(WANTED, candidates)

    assert match is not None
    assert match.track.video_id == "s"


def test_the_edition_with_the_same_explicit_flag_wins():
    wanted = MatchTrack("Song", ["Artist"], "Album", 200_000, explicit=True)
    candidates = candidates_from(
        [
            song("clean", "Song", ["Artist"], 200, "Album", explicit=False),
            song("dirty", "Song", ["Artist"], 200, "Album", explicit=True),
        ]
    )

    match = select(wanted, candidates)

    assert match is not None
    assert match.track.video_id == "dirty"


def test_a_match_knows_where_to_download_from():
    match = AudioMatch(MatchTrack("Song", video_id="abc123"), 1.0)

    assert match.url == "https://music.youtube.com/watch?v=abc123"


class FakeMatcher(YouTubeMusicMatcher):
    """A matcher whose searches answer from a table of (query, filter) -> results."""

    def __init__(self, answers, match_videos=True):
        super().__init__(match_videos)
        self.answers = answers
        self.asked = []

    def _search_blocking(self, query, kind):
        self.asked.append((query, kind))
        answer = self.answers.get((query, kind), [])
        if isinstance(answer, Exception):
            raise answer
        return answer


GOOD = song("good", "Get Lucky", ["Daft Punk", "Pharrell Williams"], 248)
QUERY = "Daft Punk Get Lucky (feat. Pharrell Williams)"


async def test_an_isrc_hit_is_taken_without_searching_further():
    matcher = FakeMatcher({("GBDUW1300136", "songs"): [GOOD]})

    match = await matcher.find(WANTED, "GBDUW1300136")

    assert match is not None
    assert match.track.video_id == "good"
    assert matcher.asked == [("GBDUW1300136", "songs")]


async def test_a_song_found_by_name_when_the_isrc_finds_nothing():
    matcher = FakeMatcher({(QUERY, "songs"): [GOOD]})

    match = await matcher.find(WANTED, "GBDUW1300136")

    assert match is not None
    assert match.track.video_id == "good"
    assert [kind for _, kind in matcher.asked] == ["songs", "songs"]


async def test_videos_are_tried_when_there_is_no_song():
    official = video("vid", "Daft Punk - Get Lucky", "Daft Punk", 248)
    matcher = FakeMatcher({(QUERY, "videos"): [official]})

    match = await matcher.find(WANTED)

    assert match is not None
    assert match.track.video_id == "vid"
    assert [kind for _, kind in matcher.asked] == ["songs", "videos"]


async def test_videos_are_left_alone_when_the_config_says_so():
    official = video("vid", "Daft Punk - Get Lucky", "Daft Punk", 248)
    matcher = FakeMatcher({(QUERY, "videos"): [official]}, match_videos=False)

    assert await matcher.find(WANTED) is None
    assert [kind for _, kind in matcher.asked] == ["songs"]


async def test_a_search_that_keeps_failing_is_no_result_not_a_crash(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("streamrip.client.ytmusic.asyncio.sleep", no_sleep)
    matcher = FakeMatcher(
        {(QUERY, "songs"): RuntimeError("blocked")}, match_videos=False
    )

    assert await matcher.find(WANTED) is None
    assert len(matcher.asked) == 2  # tried twice


@pytest.mark.parametrize("isrc", [None, ""])
async def test_without_an_isrc_only_the_name_is_searched(isrc):
    matcher = FakeMatcher({(QUERY, "songs"): [GOOD]})

    await matcher.find(WANTED, isrc)

    assert [query for query, _ in matcher.asked] == [QUERY]
