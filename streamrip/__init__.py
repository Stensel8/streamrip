lazy from . import converter, db, exceptions, media, metadata
lazy from .config import Config

__all__ = ["Config", "converter", "db", "exceptions", "media", "metadata"]
__version__ = "2.8.3"
# What streamrip says it is, where it does not pass for a browser.
USER_AGENT = f"streamrip/{__version__} (https://opencommit.eu/Stensel8/streamrip)"
