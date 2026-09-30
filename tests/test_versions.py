import re

import pytest

from streamrip import __version__ as init_version

toml_version_re = re.compile(r'version\s*\=\s*"([\d\.]+)"')


@pytest.fixture
def pyproject_version() -> str:
    with open("pyproject.toml") as f:
        m = toml_version_re.search(f.read())
    assert m is not None
    return m.group(1)


def test_streamrip_versions_match(pyproject_version):
    assert pyproject_version == init_version
