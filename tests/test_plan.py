import io
from types import SimpleNamespace

from rich.console import Console
from test_media import _album

from streamrip.rip import plan as plan_module


def planned(monkeypatch, media, seconds=600.0):
    out = io.StringIO()
    monkeypatch.setattr(plan_module, "console", Console(file=out, width=200))

    class Client:  # hashable, as the real ones are
        source = "qobuz"
        paced = True

        def pace(self, *_, **__):
            pass

        def load(self):
            return SimpleNamespace(tier="slow", this_hour=3)

        def time_for(self, _count):
            return seconds

    monkeypatch.setattr(plan_module, "planned_tracks", lambda _media: {Client(): 9})
    pacing = SimpleNamespace(pacing="auto")
    config = SimpleNamespace(session=SimpleNamespace(downloads=pacing))
    plan_module.show_plan(SimpleNamespace(media=media, config=config))
    return out.getvalue()


def test_plan_says_quality_new_tracks_and_time(monkeypatch):
    album = _album([object()] * 9)
    album.meta.album = "Blue Lines"
    album.meta.info.bit_depth, album.meta.info.sampling_rate = 24, 96
    album.skipped_tracks = 3

    text = planned(monkeypatch, [album])

    assert "Blue Lines [FLAC 24B-96kHz]: 9 tracks, 3 already downloaded" in text
    assert (
        "Qobuz: 9 tracks, about 10 min at its slow pace (3 already this hour)" in text
    )
    assert text.rstrip().endswith(
        "9 tracks would be downloaded from Qobuz. Nothing was downloaded."
    )


def test_plan_without_anything_new_says_so(monkeypatch):
    assert "Nothing to download" in planned(monkeypatch, [])
