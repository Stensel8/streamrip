"""Streamrip specific exceptions."""

import re


class AuthenticationError(Exception):
    """AuthenticationError."""


class MissingCredentialsError(Exception):
    """MissingCredentials."""


class InvalidAppIdError(Exception):
    """InvalidAppIdError."""


class InvalidAppSecretError(Exception):
    """InvalidAppSecretError."""


class NonStreamableError(Exception):
    """Item is not streamable.

    A versatile error that can have many causes.
    """


def restriction_message(code: str) -> str:
    """A streaming service's CamelCase restriction code as a readable sentence."""
    words = re.findall(r"[A-Z][a-z]+", code)
    return " ".join([words[0], *map(str.lower, words[1:])]) if words else code


class ItemNotFoundError(NonStreamableError):
    """The API returned 404 for an item.

    A subclass of NonStreamableError so existing handlers are unaffected, but
    distinguishable for callers fetching something optional -- "this does not
    exist" and "this failed to download" deserve different log levels.
    """


class FFmpegNotFoundError(Exception):
    """A download or conversion needs ffmpeg and none could be found.

    Retrying cannot help, so downloads do not retry it. The message tells the
    user how to get an ffmpeg.
    """


class ConversionError(Exception):
    """ConversionError."""


class APIError(Exception):
    """A streaming service answered a request with an error.

    The message carries the service's own explanation where it gave one.
    """


class IncompleteDownloadError(Exception):
    """A resumed download does not line up with the file the server announced."""


class TrackDownloadFailedError(Exception):
    """Raised when a track fails to download after retrying.

    Signals to Media.rip() that postprocess (tagging, marking downloaded)
    must not run for this track.
    """
