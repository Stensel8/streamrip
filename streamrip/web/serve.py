"""Serving `streamrip web`: in the terminal, or in the background.

Either way the server writes where it listens to web.json (the owner's to
read only), with a token, and what it does to web.log: `streamrip web --log`
follows that, and `--stop` asks the server itself to stop, with the token. So
no other process is hit by a process id that was given out again.
"""

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
lazy from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request
from werkzeug.serving import make_server

from ..config import APP_DIR
from ..state import write_private

STATE = APP_DIR / "web.json"
LOG = APP_DIR / "web.log"
TOKEN_HEADER = "X-Streamrip-Stop"
# Set for the server `--background` starts: its output is web.log already.
BACKGROUND = "STREAMRIP_WEB_BACKGROUND"


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}


def running() -> dict | None:
    """The server of this computer ({"pid", "port", "token"}), if it answers."""
    if not (state := _state()):
        return None
    try:
        answers = requests.get(f"http://127.0.0.1:{state['port']}/health", timeout=2).ok
    except requests.RequestException:
        answers = False
    return state if answers else None


def serve(app: Flask, host: str, port: int):
    """Serve until Ctrl+C or `streamrip web --stop`."""
    server = make_server(host, port, app, threaded=True)
    token = secrets.token_urlsafe(24)

    def stop():
        if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            abort(403)
        threading.Thread(target=server.shutdown, daemon=True).start()
        return jsonify(status="stopping")

    app.add_url_rule("/stop", "stop", stop, methods=["POST"])
    write_private(
        STATE,
        json.dumps({"pid": os.getpid(), "port": server.server_port, "token": token}),
    )
    # What it does, as plain lines: in web.log for --log, and on a server in
    # the background (whose output is web.log) instead of the terminal's columns.
    root = logging.getLogger()
    if os.environ.get(BACKGROUND):
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
        root.handlers = [handler]
    else:
        LOG.write_text("", encoding="utf-8")
        handler = logging.FileHandler(LOG, encoding="utf-8")
        root.addHandler(handler)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if _state().get("pid") == os.getpid():  # not one a later server wrote
            STATE.unlink(missing_ok=True)


def start_background(config_path: Path, host: str, port: int) -> dict:
    """Start the server as a process of its own, its output in web.log, and
    wait until it answers."""
    command = [sys.executable, "-m", "streamrip", "--config-path", str(config_path)]
    command += ["web", "--host", host, "--port", str(port), "--no-browser"]
    # No update check from a server nobody watches.
    env = os.environ | {BACKGROUND: "1", "STREAMRIP_NO_UPDATE_CHECK": "1"}
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        detach: dict = {"creationflags": flags}
    else:
        detach = {"start_new_session": True}  # it outlives the terminal
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("wb") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **detach
        )
    for _ in range(100):  # 20 seconds
        if process.poll() is not None:
            raise RuntimeError(f"It stopped at once; {LOG} says why.")
        if (state := running()) and state["pid"] == process.pid:
            return state
        time.sleep(0.2)
    raise RuntimeError(f"It did not answer within 20 seconds; {LOG} says why.")


def stop() -> bool:
    """Ask the server to stop. Whether one was running."""
    if not (state := _state()):
        return False
    try:
        url = f"http://127.0.0.1:{state['port']}/stop"
        requests.post(
            url, headers={TOKEN_HEADER: state["token"]}, timeout=5
        ).raise_for_status()
    except requests.RequestException:
        STATE.unlink(missing_ok=True)  # left by a server that is gone
        return False
    return True


def follow(lines: int = 40):
    """Print the end of web.log, then what is added to it, until Ctrl+C."""
    with LOG.open(encoding="utf-8", errors="replace") as log:
        sys.stdout.writelines(deque(log, maxlen=lines))
        while True:
            if line := log.readline():
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                time.sleep(0.5)
