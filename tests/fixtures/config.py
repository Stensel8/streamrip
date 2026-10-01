import os

import pytest

from streamrip.config import Config


@pytest.fixture()
def config():
    c = Config.defaults()
    c.session.qobuz.user_id = os.environ["QOBUZ_USER_ID"]
    c.session.qobuz.auth_token = os.environ["QOBUZ_AUTH_TOKEN"]
    return c
