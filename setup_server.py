import os
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime

import requests
from flask import Flask, jsonify, redirect, render_template

sys.path.insert(0, "/app")

SIGNAL_FILE = "/tmp/plex-claim"
CLIENT_ID = str(uuid.uuid4())
PORT = int(os.environ.get("PLEX_SETUP_PORT", 32400))

state = {
    "screen": "login",
    "pin_id": None,
    "auth_token": None,
    "token_expires_at": None,
}
state_lock = threading.Lock()

app = Flask(__name__, template_folder="/app/templates")


@app.after_request
def no_cache(resp):
    # The initializing screen must not be cached — once Plex owns 32400 and
    # the user reloads, they should land on Plex, not a stale copy of our UI.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/identity")
def identity():
    return jsonify({"MediaContainer": {"machineIdentifier": "setup", "version": "setup", "size": 0}})


@app.route("/web")
@app.route("/web/")
@app.route("/web/<path:subpath>")
def plex_web_redirect(subpath=""):
    return redirect("/")


@app.route("/")
def index():
    with state_lock:
        screen = state["screen"]
        auth_token = state["auth_token"] or ""
    return render_template("index.html", screen=screen, auth_token=auth_token)


@app.route("/api/state")
def get_state():
    with state_lock:
        expires_in = None
        if state["token_expires_at"]:
            expires_in = max(0, int(state["token_expires_at"] - time.time()))
        return jsonify({
            "screen": state["screen"],
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
        pin_id = state["pin_id"]

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

        # Get the claim token
        claim_resp = requests.get(
            "https://plex.tv/api/claim/token.json",
            headers={**headers, "X-Plex-Token": auth_token},
            timeout=10,
        )
        claim_resp.raise_for_status()
        claim_token = claim_resp.json().get("token", "")

        with state_lock:
            state["screen"] = "initializing"
            state["auth_token"] = auth_token

        # Write signal file — the s6 init script is polling for this
        with open(SIGNAL_FILE, "w") as f:
            f.write(claim_token)

        return jsonify({"ok": True, "done": True, "auth_token": auth_token})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/auth/restart", methods=["POST"])
def auth_restart():
    with state_lock:
        state["screen"] = "login"
        state["pin_id"] = None
        state["auth_token"] = None
        state["token_expires_at"] = None
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
