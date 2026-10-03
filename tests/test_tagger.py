import os
import shutil

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from util import arun

from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
    tag_file,
)

TEST_FLAC_ORIGINAL = "tests/silence.flac"
TEST_FLAC_COPY = "tests/silence_copy.flac"
TEST_M4A = "tests/silence.m4a"
test_cover = "tests/1x1_pixel.jpg"


def wipe_test_flac():
    audio = FLAC(TEST_FLAC_COPY)
    # Remove all tags
    audio.delete()
    audio.save()


@pytest.fixture()
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


def test_tag_flac_no_cover(sample_metadata):
    shutil.copy(TEST_FLAC_ORIGINAL, TEST_FLAC_COPY)
    wipe_test_flac()
    arun(tag_file(TEST_FLAC_COPY, sample_metadata, None))
    file = FLAC(TEST_FLAC_COPY)
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
    os.remove(TEST_FLAC_COPY)


def test_tag_flac_cover(sample_metadata):
    shutil.copy(TEST_FLAC_ORIGINAL, TEST_FLAC_COPY)
    wipe_test_flac()
    arun(tag_file(TEST_FLAC_COPY, sample_metadata, test_cover))
    file = FLAC(TEST_FLAC_COPY)
    assert file["title"][0] == "testtitle"
    with open(test_cover, "rb") as img:
        assert file.pictures[0].data == img.read()
    os.remove(TEST_FLAC_COPY)


def test_tag_flac_multiple_artists(sample_metadata):
    # Tracks with several credited artists must land as
    # separate ARTIST fields, not one "A, B" string a player has to
    # re-split on its own.
    sample_metadata.artists = ["The Kid LAROI", "Lil Mosey"]
    shutil.copy(TEST_FLAC_ORIGINAL, TEST_FLAC_COPY)
    wipe_test_flac()
    arun(tag_file(TEST_FLAC_COPY, sample_metadata, None))
    file = FLAC(TEST_FLAC_COPY)
    assert list(file["artist"]) == ["The Kid LAROI", "Lil Mosey"]
    os.remove(TEST_FLAC_COPY)


def test_tag_flac_multiple_album_artists(sample_metadata):
    # The same for an album credited to several artists: one "A, B, C"
    # ALBUMARTIST shows up in a library as a single artist of that name.
    sample_metadata.album.albumartists = ["Slander", "Spiritbox", "Vastive"]
    shutil.copy(TEST_FLAC_ORIGINAL, TEST_FLAC_COPY)
    wipe_test_flac()
    arun(tag_file(TEST_FLAC_COPY, sample_metadata, None))
    file = FLAC(TEST_FLAC_COPY)
    assert list(file["albumartist"]) == ["Slander", "Spiritbox", "Vastive"]
    os.remove(TEST_FLAC_COPY)


def test_tag_m4a(sample_metadata, tmp_path):
    path = str(tmp_path / "track.m4a")
    shutil.copy(TEST_M4A, path)
    sample_metadata.artists = ["A", "B"]
    arun(tag_file(path, sample_metadata, test_cover))
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


def test_tag_mp3(sample_metadata, tmp_path):
    path = str(tmp_path / "track.mp3")
    open(path, "wb").close()  # an ID3 tag doesn't need an audio stream
    sample_metadata.artists = ["A", "B"]
    sample_metadata.album.compilation = "1"
    arun(tag_file(path, sample_metadata, None))
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


def test_exclude_only_drops_the_named_tag(sample_metadata, tmp_path):
    # Excluding the composer used to drop the year from M4A files too, as
    # both were mapped onto the same atom.
    path = str(tmp_path / "track.m4a")
    shutil.copy(TEST_M4A, path)
    arun(tag_file(path, sample_metadata, test_cover, exclude=["composer", "cover"]))
    tags = MP4(path).tags
    assert "\xa9wrt" not in tags
    assert "covr" not in tags
    assert tags["\xa9day"] == ["1999"]


@pytest.mark.parametrize("ext", ["flac", "m4a", "mp3"])
def test_retagging_keeps_a_single_cover(sample_metadata, tmp_path, ext):
    # A converted file is tagged a second time, and ffmpeg may already have
    # copied the cover into it. FLAC's add_picture appends where ID3 and MP4
    # replace, so a re-tagged FLAC used to carry the cover twice.
    path = str(tmp_path / f"track.{ext}")
    if ext == "flac":
        shutil.copy(TEST_FLAC_ORIGINAL, path)
    elif ext == "m4a":
        shutil.copy(TEST_M4A, path)
    else:
        open(path, "wb").close()
    arun(tag_file(path, sample_metadata, test_cover))
    arun(tag_file(path, sample_metadata, test_cover))
    if ext == "flac":
        covers = FLAC(path).pictures
    elif ext == "m4a":
        covers = MP4(path).tags["covr"]
    else:
        covers = ID3(path).getall("APIC")
    assert len(covers) == 1
