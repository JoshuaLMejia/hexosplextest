import os
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime

import requests
from flask import Flask, jsonify, render_template, request

sys.path.insert(0, "/app")

from hexos_hooks import HookContext
import plex_hook

PLEX_URL = "http://localhost:32400"
SIGNAL_FILE = "/tmp/plex-claim"
CLIENT_ID = str(uuid.uuid4())
PORT = int(os.environ.get("PLEX_SETUP_PORT", 32400))

# State machine values: "login" | "waiting_auth" | "initializing" | "done" | "error"
state = {
    "screen": "login",
    "logs": deque(maxlen=200),
    "pin_id": None,
    "auth_token": None,
    "claim_token": None,
    "error": None,
    "token_expires_at": None,
}
state_lock = threading.Lock()

app = Flask(__name__, template_folder="/app/templates")


def _add_log(msg: str):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "message": msg}
    with state_lock:
        state["logs"].appendleft(entry)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/identity")
def identity():
    return jsonify({"MediaContainer": {"machineIdentifier": "setup", "version": "setup"}})


@app.route("/")
def index():
    with state_lock:
        screen = state["screen"]
    return render_template("index.html", screen=screen)


@app.route("/api/state")
def get_state():
    with state_lock:
        expires_in = None
        if state["token_expires_at"]:
            expires_in = max(0, int(state["token_expires_at"] - time.time()))
        return jsonify({
            "screen": state["screen"],
            "logs": list(state["logs"])[:50],
            "error": state["error"],
            "expires_in": expires_in,
        })


@app.route("/api/auth/start", methods=["POST"])
def auth_start():
    headers = {
        "X-Plex-Client-Identifier": CLIENT_ID,
        "X-Plex-Product": "HexOS Plex Setup",
        "X-Plex-Version": "1.0",
        "Accept": "application/json",
    }
    try:
        resp = requests.post(
            "https://plex.tv/api/v2/pins",
            headers=headers,
            params={"strong": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        pin_id = data["id"]
        code = data["code"]

        with state_lock:
            state["pin_id"] = pin_id
            state["screen"] = "waiting_auth"
            # Plex claim tokens expire in ~4 minutes; PIN itself expires later but
            # we surface the 4-min window since that's the binding constraint.
            state["token_expires_at"] = time.time() + 240

        auth_url = (
            f"https://app.plex.tv/auth#?"
            f"clientID={CLIENT_ID}"
            f"&code={code}"
            f"&context%5Bdevice%5D%5Bproduct%5D=HexOS+Plex+Setup"
        )
        return jsonify({"ok": True, "auth_url": auth_url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/auth/poll")
def auth_poll():
    with state_lock:
        screen = state["screen"]
        pin_id = state["pin_id"]
        existing_claim = state["claim_token"]

    if screen == "done":
        return jsonify({"ok": True, "done": True})

    if existing_claim:
        return jsonify({"ok": True, "done": True})

    if not pin_id:
        return jsonify({"ok": False, "error": "No auth in progress"}), 400

    headers = {
        "X-Plex-Client-Identifier": CLIENT_ID,
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            f"https://plex.tv/api/v2/pins/{pin_id}",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        auth_token = resp.json().get("authToken")
        if not auth_token:
            return jsonify({"ok": True, "done": False})

        claim_resp = requests.get(
            "https://plex.tv/api/claim/token.json",
            headers={**headers, "X-Plex-Token": auth_token},
            timeout=10,
        )
        claim_resp.raise_for_status()
        claim_token = claim_resp.json().get("token", "")

        with state_lock:
            state["auth_token"] = auth_token
            state["claim_token"] = claim_token
            state["screen"] = "initializing"

        # Write the signal file so the s6 cont-init script unblocks Plex startup
        with open(SIGNAL_FILE, "w") as f:
            f.write(claim_token)

        # Run the post-install hook in the background
        threading.Thread(target=_run_post_install, daemon=True).start()

        return jsonify({"ok": True, "done": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/auth/restart", methods=["POST"])
def auth_restart():
    """Let the user restart the PIN flow if the claim token expired."""
    with state_lock:
        state["screen"] = "login"
        state["pin_id"] = None
        state["auth_token"] = None
        state["claim_token"] = None
        state["token_expires_at"] = None
        state["error"] = None
        state["logs"].clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Post-install hook runner
# ---------------------------------------------------------------------------

def _run_post_install():
    _add_log("Plex is starting up...")

    ctx_data = {
        "appId": "plex",
        "event": "onAfterInstall",
        "containerName": os.environ.get("PLEX_CONTAINER_NAME", ""),
        "mounts": {
            "/movies": os.environ.get("PLEX_MOVIES_PATH", "/movies"),
            "/shows": os.environ.get("PLEX_SHOWS_PATH", "/shows"),
        },
        "truenasApiKey": os.environ.get("TRUENAS_API_KEY", ""),
        "truenasRestUrl": os.environ.get("TRUENAS_REST_URL", "http://localhost/api/v2.0"),
        "_log_callback": _add_log,
    }

    ctx = HookContext(ctx_data)

    try:
        plex_hook.after_install(ctx)
        with state_lock:
            state["screen"] = "done"
    except Exception as e:
        _add_log(f"Setup error: {e}")
        with state_lock:
            state["screen"] = "error"
            state["error"] = str(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
