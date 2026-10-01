import os

import pytest
from util import arun

from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config


@pytest.fixture(scope="session")
def qobuz_client():
    config = Config.defaults()
    config.session.qobuz.user_id = os.environ["QOBUZ_USER_ID"]
    config.session.qobuz.auth_token = os.environ["QOBUZ_AUTH_TOKEN"]
    if "QOBUZ_APP_ID" in os.environ and "QOBUZ_SECRETS" in os.environ:
        config.session.qobuz.app_id = os.environ["QOBUZ_APP_ID"]
        config.session.qobuz.secrets = os.environ["QOBUZ_SECRETS"].split(",")
    client = QobuzClient(config)
    arun(client.login())

    yield client

    arun(client.session.close())
