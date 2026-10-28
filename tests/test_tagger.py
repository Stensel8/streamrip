import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
    tag_file,
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
