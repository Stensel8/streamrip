"""Ending streamrip when it is sent SIGTERM."""


def terminate(signum, frame):
    """End the process the way SIGTERM does, but letting `finally` blocks run."""
    raise SystemExit(128 + signum)
