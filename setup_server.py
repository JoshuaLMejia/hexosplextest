import os
import re
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime

import requests
from flask import Flask, jsonify, redirect, render_template, request

sys.path.insert(0, "/app")

from hexos_hooks import HookContext
import plex_hook

PLEX_URL = "http://localhost:32400"
SIGNAL_FILE = "/tmp/plex-claim"
PREFS_PATH = "/config/Library/Application Support/Plex Media Server/Preferences.xml"
CLIENT_ID = str(uuid.uuid4())
PORT = int(os.environ.get("PLEX_SETUP_PORT", 32400))

# State machine: "login" | "waiting_auth" | "initializing" | "done" | "error"
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
    # Stub for TrueNAS health checks. "setup" identifier is used by plex_hook
    # to detect that Flask (not real Plex) is still answering on this port.
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

        # Exchange claim token (short-lived) for the permanent online token
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

        # Delay before writing signal so browser receives this response and
        # renders the initializing screen before s6 kills Flask
        threading.Thread(target=_prepare_and_signal, args=(auth_token, claim_token), daemon=True).start()

        return jsonify({"ok": True, "done": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/auth/restart", methods=["POST"])
def auth_restart():
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
# Core setup logic
# ---------------------------------------------------------------------------

def _prepare_and_signal(auth_token: str, claim_token: str):
    """
    Runs in background after OAuth completes.
    1. Exchanges claim token for PlexOnlineToken via plex.tv API
    2. Writes PlexOnlineToken + EULA flags directly into Preferences.xml
    3. Signals s6 to kill Flask and start Plex
    4. Waits for real Plex to come up, then creates libraries
    """
    # Give browser time to receive the poll response and render initializing screen
    time.sleep(5)

    _add_log("Exchanging claim token for server token...")
    online_token = _exchange_claim_token(claim_token)

    if online_token:
        _add_log("Writing token and preferences to disk...")
        _patch_preferences_xml(online_token)
    else:
        # Fall back to passing claim token via signal file and letting
        # pms-docker's 40-plex-first-run handle it on next start — but
        # since first-run already ran, this won't work. Log the warning.
        _add_log("Warning: could not exchange claim token. Plex wizard may appear.")

    # Signal s6 to start Plex (write signal file — content unused now but kept for compatibility)
    _add_log("Starting Plex...")
    with open(SIGNAL_FILE, "w") as f:
        f.write(claim_token)

    # Run post-install hook (creates libraries) after Plex is up
    _run_post_install()


def _exchange_claim_token(claim_token: str) -> str:
    """
    Exchanges the short-lived claim token for the permanent PlexOnlineToken
    by calling the plex.tv claim exchange endpoint directly.
    Returns the PlexOnlineToken string, or empty string on failure.
    """
    try:
        # Get the machine identifier that pms-docker already wrote
        machine_id = _read_machine_identifier()
        if not machine_id:
            _add_log("Could not read MachineIdentifier from Preferences.xml")
            return ""

        resp = requests.post(
            "https://plex.tv/api/claim/exchange",
            headers={
                "X-Plex-Client-Identifier": machine_id,
                "Accept": "application/json",
            },
            params={"token": claim_token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        token = (
            data.get("authToken")
            or data.get("user", {}).get("authToken")
            or data.get("token")
            or ""
        )
        if token:
            _add_log("Server token obtained.")
        return token
    except Exception as e:
        _add_log(f"Claim exchange error: {e}")
        return ""


def _read_machine_identifier() -> str:
    """Reads MachineIdentifier from the Preferences.xml that pms-docker wrote."""
    try:
        with open(PREFS_PATH, "r") as f:
            contents = f.read()
        match = re.search(r'MachineIdentifier="([^"]+)"', contents)
        return match.group(1) if match else ""
    except (FileNotFoundError, IOError):
        return ""


def _patch_preferences_xml(online_token: str):
    """
    Writes PlexOnlineToken, AcceptedEULA, PublishServerOnPlexOnlineKey, and
    FriendlyName directly into Preferences.xml before Plex starts.
    Plex reads this file at startup — having PlexOnlineToken present means
    it will skip the first-run wizard entirely.
    """
    try:
        with open(PREFS_PATH, "r") as f:
            contents = f.read()

        # Parse and patch attributes
        tree = ET.parse(PREFS_PATH)
        root = tree.getroot()
        root.set("PlexOnlineToken", online_token)
        root.set("AcceptedEULA", "1")
        root.set("PublishServerOnPlexOnlineKey", "1")
        root.set("FriendlyName", "HexOS Plex")

        tree.write(PREFS_PATH, encoding="utf-8", xml_declaration=True)
        _add_log("Preferences written.")
    except Exception as e:
        _add_log(f"Error patching Preferences.xml: {e}")


def _run_post_install():
    _add_log("Waiting for Plex to start...")

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
