import shutil
import threading
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TSSE
from mutagen.mp4 import MP4

from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
    tag_file,
    tagger,
)

TESTS = Path(__file__).parent
TEST_FLAC = TESTS / "silence.flac"
TEST_M4A = TESTS / "silence.m4a"
TEST_COVER = TESTS / "1x1_pixel.jpg"


@pytest.fixture
def flac_path(tmp_path) -> Path:
    """A copy of the silent FLAC with every tag removed."""
    path = tmp_path / "track.flac"
    shutil.copy(TEST_FLAC, path)
    audio = FLAC(path)
    audio.delete()
    audio.save()
    return path


@pytest.fixture
def sample_metadata() -> TrackMetadata:
    return TrackMetadata(
        TrackInfo(id="12345", explicit=True),
        "testtitle",
        AlbumMetadata(
            AlbumInfo("5678", 4, "flac"),
            "testalbum",
            "testalbumartist",
            "1999",
            ["rock", "pop"],
            Covers(),
            tracktotal=14,
            disctotal=3,
            albumcomposer="testalbumcomposer",
            copyright="(c) stuff (p) other stuff",
            date="1998-02-13",
            description="testdesc",
        ),
        "testartist",
        3,
        1,
        "testcomposer",
        isrc="USABC1234567",
    )


async def test_tag_flac_no_cover(sample_metadata, flac_path):
    await tag_file(flac_path, sample_metadata, None)
    file = FLAC(flac_path)
    assert file["title"][0] == "testtitle"
    assert file["album"][0] == "testalbum"
    assert file["composer"][0] == "testcomposer"
    assert file["artist"][0] == "testartist"
    assert file["albumartist"][0] == "testalbumartist"
    assert file["year"][0] == "1999"
    assert file["genre"][0] == "rock, pop"
    assert file["tracknumber"][0] == "03"
    assert file["discnumber"][0] == "01"
    assert file["copyright"][0] == "© stuff ℗ other stuff"
    assert file["tracktotal"][0] == "14"
    assert file["date"][0] == "1998-02-13"
    assert file["description"][0] == "testdesc"
    assert file["isrc"][0] == "USABC1234567"
    # Empty lyrics (the default) are left out, not written as "".
    assert "lyrics" not in file


async def test_tag_flac_cover(sample_metadata, flac_path):
    await tag_file(flac_path, sample_metadata, TEST_COVER)
    file = FLAC(flac_path)
    assert file["title"][0] == "testtitle"
    assert file.pictures[0].data == TEST_COVER.read_bytes()


async def test_tag_flac_cover_carries_its_own_dimensions(sample_metadata, flac_path):
    # Left at Mutagen's default of 0, some players take that as "no usable
    # cover" without decoding the JPEG themselves to find out.
    await tag_file(flac_path, sample_metadata, TEST_COVER)
    picture = FLAC(flac_path).pictures[0]
    assert (picture.width, picture.height, picture.depth) == (1, 1, 24)


async def test_tag_flac_multiple_artists(sample_metadata, flac_path):
    """Several credited artists land as separate ARTIST fields.

    Not as one "A, B" string that a player has to split on its own.
    """
    sample_metadata.artists = ["The Kid LAROI", "Lil Mosey"]
    await tag_file(flac_path, sample_metadata, None)
    assert list(FLAC(flac_path)["artist"]) == ["The Kid LAROI", "Lil Mosey"]


async def test_tag_flac_multiple_album_artists(sample_metadata, flac_path):
    """The same for an album credited to several artists.

    One "A, B, C" ALBUMARTIST shows up in a library as a single artist of that name.
    """
    sample_metadata.album.albumartists = ["Slander", "Spiritbox", "Vastive"]
    await tag_file(flac_path, sample_metadata, None)
    assert list(FLAC(flac_path)["albumartist"]) == ["Slander", "Spiritbox", "Vastive"]


async def test_tag_m4a(sample_metadata, tmp_path):
    path = tmp_path / "track.m4a"
    shutil.copy(TEST_M4A, path)
    sample_metadata.artists = ["A", "B"]
    await tag_file(path, sample_metadata, TEST_COVER)
    tags = MP4(path).tags
    assert tags["\xa9nam"] == ["testtitle"]
    assert tags["\xa9ART"] == ["A", "B"]
    assert tags["aART"] == ["testalbumartist"]
    # The composer used to be mapped onto the year's atom and lost.
    assert tags["\xa9wrt"] == ["testcomposer"]
    assert tags["\xa9day"] == ["1999"]
    assert tags["trkn"] == [(3, 14)]
    assert tags["disk"] == [(1, 3)]
    assert tags["cprt"] == ["© stuff ℗ other stuff"]
    assert tags["desc"] == ["testdesc"]
    assert bytes(tags["----:com.apple.iTunes:ISRC"][0]) == b"USABC1234567"
    assert "cpil" not in tags
    assert "\xa9lyr" not in tags
    assert len(tags["covr"]) == 1


async def test_tag_mp3(sample_metadata, tmp_path):
    path = tmp_path / "track.mp3"
    path.touch()  # an ID3 tag doesn't need an audio stream
    sample_metadata.artists = ["A", "B"]
    sample_metadata.album.compilation = "1"
    await tag_file(path, sample_metadata, None)
    tags = ID3(path, translate=False)  # as written, not upgraded to v2.4
    assert tags.version[:2] == (2, 3)
    assert tags["TIT2"].text == ["testtitle"]
    # ID3v2.3 has no multi-value text frames; "/" is its separator.
    assert tags["TPE1"].text == ["A/B"]
    assert tags["TCOM"].text == ["testcomposer"]
    assert tags["TYER"].text == ["1999"]
    assert tags["TRCK"].text == ["3/14"]
    assert tags["TPOS"].text == ["1/3"]
    assert tags["TSRC"].text == ["USABC1234567"]
    assert tags["TCMP"].text == ["1"]
    # The album description used to land in TIT1 (grouping), and empty
    # lyrics in an empty USLT frame.
    assert "TIT1" not in tags
    assert not tags.getall("USLT")


async def test_exclude_only_drops_the_named_tag(sample_metadata, tmp_path):
    # Excluding the composer used to drop the year from M4A files too, as
    # both were mapped onto the same atom.
    path = tmp_path / "track.m4a"
    shutil.copy(TEST_M4A, path)
    await tag_file(path, sample_metadata, TEST_COVER, exclude=["composer", "cover"])
    tags = MP4(path).tags
    assert "\xa9wrt" not in tags
    assert "covr" not in tags
    assert tags["\xa9day"] == ["1999"]


@pytest.mark.parametrize("ext", ["flac", "m4a", "mp3"])
async def test_an_earlier_tool_s_tag_does_not_survive_retagging(
    sample_metadata, tmp_path, ext
):
    # A remux (Deezer's download goes through ffmpeg) leaves its own
    # "encoder" tag on the file; tag_file's set of tags is meant to be the
    # whole, authoritative story, so nothing a prior pass wrote should linger.
    path = tmp_path / f"track.{ext}"
    if ext == "flac":
        shutil.copy(TEST_FLAC, path)
        audio = FLAC(path)
        audio["encoder"] = ["Lavf60.3.100"]
        audio.save()
    elif ext == "m4a":
        shutil.copy(TEST_M4A, path)
        audio = MP4(path)
        audio.tags["\xa9too"] = ["Lavf60.3.100"]
        audio.save()
    else:
        path.touch()
        audio = ID3()
        audio.add(TSSE(encoding=3, text="Lavf60.3.100"))
        audio.save(path)

    await tag_file(path, sample_metadata, None)

    if ext == "flac":
        assert "encoder" not in FLAC(path)
    elif ext == "m4a":
        assert "\xa9too" not in MP4(path).tags
    else:
        assert not ID3(path).getall("TSSE")


async def test_tag_flac_without_a_tag_block(sample_metadata, tmp_path):
    # A FLAC with no Vorbis comment block at all has mutagen's tags at None:
    # clearing them raised AttributeError, failing every Tidal and Qobuz
    # track in 2.6.7 and 2.6.8 (issue #91).
    path = tmp_path / "track.flac"
    shutil.copy(TEST_FLAC, path)
    audio = FLAC(path)
    audio.metadata_blocks = [b for b in audio.metadata_blocks if b.code != 4]
    audio.tags = None
    audio.save()
    assert FLAC(path).tags is None

    await tag_file(path, sample_metadata, TEST_COVER)

    file = FLAC(path)
    assert file["title"] == ["testtitle"]
    assert len(file.pictures) == 1


@pytest.mark.parametrize("ext", ["flac", "m4a", "mp3"])
async def test_retagging_keeps_a_single_cover(sample_metadata, tmp_path, ext):
    # A converted file is tagged a second time, and ffmpeg may already have
    # copied the cover into it. FLAC's add_picture appends where ID3 and MP4
    # replace, so a re-tagged FLAC used to carry the cover twice.
    path = tmp_path / f"track.{ext}"
    if ext == "flac":
        shutil.copy(TEST_FLAC, path)
    elif ext == "m4a":
        shutil.copy(TEST_M4A, path)
    else:
        path.touch()
    await tag_file(path, sample_metadata, TEST_COVER)
    await tag_file(path, sample_metadata, TEST_COVER)
    if ext == "flac":
        covers = FLAC(path).pictures
    elif ext == "m4a":
        covers = MP4(path).tags["covr"]
    else:
        covers = ID3(path).getall("APIC")
    assert len(covers) == 1


@pytest.mark.parametrize("ext", ["flac", "mp3", "m4a"])
async def test_label_barcode_release_type_bpm_key_credits_and_replaygain(
    sample_metadata, tmp_path, ext
):
    # Where MusicBrainz Picard puts them, so players and Navidrome read them.
    path = tmp_path / f"track.{ext}"
    if ext == "flac":
        shutil.copy(TEST_FLAC, path)
    elif ext == "m4a":
        shutil.copy(TEST_M4A, path)
    else:
        path.touch()
    m = sample_metadata
    m.album.info.label, m.album.barcode, m.album.releasetype = "Label", "0123", "ep"
    m.composers, m.composer = ["C1", "C2"], "C1, C2"
    m.lyricists, m.bpm, m.key = ["L1", "L2"], 120, "F#m"
    m.replaygain_gain, m.replaygain_peak = -7.08, 0.936676
    await tag_file(path, m, None)

    if ext == "flac":
        t = FLAC(path)
        got = {
            k: t[k] for k in ("label", "barcode", "releasetype", "bpm", "initialkey")
        }
        assert got == {
            "label": ["Label"],
            "barcode": ["0123"],
            "releasetype": ["ep"],
            "bpm": ["120"],
            "initialkey": ["F#m"],
        }
        assert (t["composer"], t["lyricist"]) == (["C1", "C2"], ["L1", "L2"])
        assert t["itunesadvisory"] == ["1"]
        assert t["replaygain_track_gain"] == ["-7.08 dB"]
        assert t["replaygain_track_peak"] == ["0.936676"]
    elif ext == "mp3":
        t = ID3(path, translate=False)
        assert t["TPUB"].text == ["Label"]
        assert t["TXXX:BARCODE"].text == ["0123"]
        assert t["TXXX:MusicBrainz Album Type"].text == ["ep"]
        assert (t["TBPM"].text, t["TKEY"].text) == (["120"], ["F#m"])
        assert (t["TCOM"].text, t["TEXT"].text) == (["C1/C2"], ["L1/L2"])
        assert t["TXXX:ITUNESADVISORY"].text == ["1"]
        assert t["TXXX:REPLAYGAIN_TRACK_GAIN"].text == ["-7.08 dB"]
        assert t["TXXX:REPLAYGAIN_TRACK_PEAK"].text == ["0.936676"]
    else:
        t = MP4(path).tags
        free = "----:com.apple.iTunes:"
        assert t[free + "LABEL"] == [b"Label"]
        assert t[free + "BARCODE"] == [b"0123"]
        assert t[free + "MusicBrainz Album Type"] == [b"ep"]
        assert (t["tmpo"], t[free + "initialkey"]) == ([120], [b"F#m"])
        assert (t["\xa9wrt"], t[free + "LYRICIST"]) == (["C1", "C2"], [b"L1", b"L2"])
        assert t["rtng"] == [1]
        assert t[free + "replaygain_track_gain"] == [b"-7.08 dB"]
        assert t[free + "replaygain_track_peak"] == [b"0.936676"]


async def test_a_clean_track_gets_no_advisory_and_unknown_values_stay_out(
    sample_metadata, flac_path
):
    sample_metadata.info.explicit = False
    await tag_file(flac_path, sample_metadata, None)
    t = FLAC(flac_path)
    for name in ("itunesadvisory", "bpm", "label", "replaygain_track_gain"):
        assert name not in t


async def test_unknown_year_and_impossible_totals_are_not_tagged(
    sample_metadata, flac_path
):
    album = sample_metadata.album
    album.year, album.date = "Unknown", None
    album.tracktotal, album.disctotal = 1, 0  # fallbacks for track 3 of disc 1
    await tag_file(flac_path, sample_metadata, None)
    t = FLAC(flac_path)
    for name in ("date", "year", "tracktotal", "totaltracks", "disctotal"):
        assert name not in t, name
    assert t["tracknumber"] == ["03"]


async def test_tagging_runs_off_the_event_loop(monkeypatch, sample_metadata, flac_path):
    """A FLAC whose tags outgrow their room is rewritten whole: not on the loop."""
    threads = []
    monkeypatch.setattr(
        tagger, "_tag_file", lambda *_: threads.append(threading.current_thread())
    )
    await tag_file(flac_path, sample_metadata, None)
    assert threads
    assert threads[0] is not threading.main_thread()
