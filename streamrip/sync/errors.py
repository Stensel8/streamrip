"""Failures that can be explained to the user.

The CLI prints them, the web interface answers with `http_status`.
"""


class SyncError(Exception):
    """A problem whose message is safe to show to the user. Anything else is a
    bug and fails loudly."""

    http_status = 400  # what the web interface answers with


class ConfigError(SyncError):
    """The setup is incomplete or unreadable."""


class CsvError(SyncError, ValueError):
    """A CSV file could not be read. A ValueError too, which is what the
    download side of streamrip catches for a CSV list it cannot read."""


class ProviderError(SyncError):
    """A service cannot do what was asked, for example reading a playlist that
    is not yours."""


class LoginError(SyncError):
    """Logging in to a service failed."""


class NotLoggedInError(LoginError):
    """There is no valid login for the service (yet, or any more)."""

    http_status = 401


class NetworkError(SyncError):
    """A service could not be reached."""

    http_status = 502


class ApiError(SyncError):
    """A service answered with an error status."""

    http_status = 502

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class QuotaExceededError(ApiError):
    """The app has used up its quota; retrying will not help."""
