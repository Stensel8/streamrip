import pytest

from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.exceptions import MissingCredentialsError


@pytest.mark.login
async def test_client_raises_missing_credentials():
    c = Config.defaults()
    with pytest.raises(MissingCredentialsError):
        await QobuzClient(c).login()
