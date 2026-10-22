import re
from pathlib import Path

import pytest

from streamrip import __version__ as init_version

toml_version_re = re.compile(r'version\s*\=\s*"([\d\.]+)"')


@pytest.fixture
def pyproject_version() -> str:
    m = toml_version_re.search(Path("pyproject.toml").read_text())
    assert m is not None
    return m.group(1)


def test_streamrip_versions_match(pyproject_version):
    assert pyproject_version == init_version
