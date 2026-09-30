import pytest

import streamrip.media.semaphore as semaphore_module


@pytest.fixture(autouse=True)
def _reset_global_download_semaphore():
    """`global_download_semaphore` caches one max_connections value for the
    whole process, by design: a real streamrip run has exactly one config.
    Tests construct many different Config instances, so without a reset the
    first test to touch it locks in its value and every later test using a
    different max_connections fails its consistency assert.
    """
    semaphore_module._global_semaphore = None
    yield
    semaphore_module._global_semaphore = None
