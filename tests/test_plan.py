import io
from types import SimpleNamespace

from rich.console import Console
from test_media import _album

from streamrip.rip import plan as plan_module


def planned(monkeypatch, media, seconds=600.0, this_hour=3):
    out = io.StringIO()
    monkeypatch.setattr(plan_module, "console", Console(file=out, width=200))

    class Client:  # hashable, as the real ones are
        source = "qobuz"

        def pace(self, *_):
            pass

        def load(self):
            return SimpleNamespace(tier="slow", this_hour=this_hour)

        def time_for(self, _count):
            return seconds

    client = Client()
    monkeypatch.setattr(plan_module, "planned_tracks", lambda _media: {client: 9})
    main = SimpleNamespace(
        media=media,
        config=SimpleNamespace(
            session=SimpleNamespace(downloads=SimpleNamespace(pacing="auto"))
        ),
    )
    plan_module.show_plan(main)
    return out.getvalue()


def test_plan_says_quality_new_tracks_time_and_that_nothing_was_downloaded(monkeypatch):
    album = _album([object()] * 9)
    album.meta.album = "Blue Lines"
    album.meta.info.bit_depth, album.meta.info.sampling_rate = 24, 96
    album.skipped_tracks = 3
    text = planned(monkeypatch, [album])
    assert "Blue Lines [FLAC 24B-96kHz]: 9 tracks, 3 already downloaded" in text
    assert (
        "Qobuz: 9 tracks, about 10 min at its slow pace; 3 tracks started this hour"
        in text
    )
    assert text.rstrip().endswith("Nothing was downloaded.")


def test_a_full_speed_account_has_no_time_to_quote(monkeypatch):
    text = planned(monkeypatch, [_album([object()])], seconds=None, this_hour=1)
    assert "at full speed; 1 track started this hour" in text


def test_plan_without_anything_new_says_so(monkeypatch):
    assert "Nothing to download" in planned(monkeypatch, [])
