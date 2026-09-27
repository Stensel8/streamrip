import sys
import types
from unittest.mock import patch

from streamrip.utils.ffmpeg_utils import find_ffmpeg


def _clear_cache():
    find_ffmpeg.cache_clear()


def test_prefers_ffmpeg_on_path():
    _clear_cache()
    with patch("shutil.which", return_value="/usr/bin/ffmpeg") as which:
        assert find_ffmpeg() == "/usr/bin/ffmpeg"
    which.assert_called_once_with("ffmpeg")


def test_falls_back_to_imageio_ffmpeg_when_not_on_path():
    _clear_cache()
    fake_module = types.ModuleType("imageio_ffmpeg")
    fake_module.get_ffmpeg_exe = lambda: "/fake/imageio_ffmpeg/ffmpeg"
    with (
        patch("shutil.which", return_value=None),
        patch.dict(sys.modules, {"imageio_ffmpeg": fake_module}),
    ):
        assert find_ffmpeg() == "/fake/imageio_ffmpeg/ffmpeg"


def test_returns_none_when_nothing_is_available():
    _clear_cache()
    with (
        patch("shutil.which", return_value=None),
        patch.dict(sys.modules, {"imageio_ffmpeg": None}),
    ):
        assert find_ffmpeg() is None
