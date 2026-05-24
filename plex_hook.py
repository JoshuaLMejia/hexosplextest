import re
import time
import requests
from hexos_hooks import HookContext

PLEX_URL = "http://localhost:32400"
PREFS_PATH = "/config/Library/Application Support/Plex Media Server/Preferences.xml"

LIBRARIES = [
    {
        "name": "Movies",
        "type": "movie",
        "location": "/movies",
        "agent": "tv.plex.agents.movie",
        "scanner": "Plex Movie",
    },
    {
        "name": "TV Shows",
        "type": "show",
        "location": "/shows",
        "agent": "tv.plex.agents.series",
        "scanner": "Plex TV Series",
    },
]


def after_install(ctx: HookContext):
    # Give the s6 script time to kill Flask and let Plex bind to 32400
    ctx.log("Waiting for Plex to start up...")
    time.sleep(10)

    token = _wait_for_token(ctx)
    if not token:
        raise RuntimeError("PlexOnlineToken missing. Did Plex start correctly?")

    _wait_for_ready(ctx)
    _set_preferences(token, ctx)
    _create_libraries(token, ctx)
    ctx.log("Setup complete.")


def _wait_for_ready(ctx: HookContext):
    ctx.log("Waiting for Plex to start...")
    interval = 5.0
    for attempt in range(1, 40):
        try:
            resp = requests.get(f"{PLEX_URL}/identity", timeout=5)
            if resp.status_code == 200:
                # Reject our own Flask stub — it returns {"MediaContainer": {"machineIdentifier": "setup"}}
                # Real Plex returns a much longer identifier and includes "size" and "claimed" fields
                data = resp.json()
                machine_id = data.get("MediaContainer", {}).get("machineIdentifier", "")
                if machine_id and machine_id != "setup":
                    ctx.log("Plex API is ready.")
                    return
        except requests.RequestException:
            pass

        time.sleep(interval)
        interval = min(interval + 2.0, 15.0)

    raise RuntimeError("Plex API did not become ready after waiting.")


def _wait_for_token(ctx: HookContext):
    # We're running inside the container — read Preferences.xml directly
    for attempt in range(1, 21):
        try:
            with open(PREFS_PATH, "r") as f:
                contents = f.read()
            match = re.search(r'PlexOnlineToken="([^"]+)"', contents)
            if match:
                ctx.log("Plex account token found.")
                return match.group(1)
        except (FileNotFoundError, IOError):
            pass

        if attempt == 1:
            ctx.log("Waiting for Preferences.xml to be written...")

        time.sleep(5)

    return None


def _set_preferences(token: str, ctx: HookContext):
    prefs = [
        {"AcceptedEULA": 1},
        {"PublishServerOnPlexOnlineKey": 1},
        {"FriendlyName": "HexOS Plex"},
    ]
    for params in prefs:
        key = list(params.keys())[0]
        params["X-Plex-Token"] = token
        for attempt in range(1, 4):
            try:
                resp = requests.put(f"{PLEX_URL}/:/prefs", params=params, timeout=5)
                if resp.ok:
                    ctx.log(f"Set preference: {key}")
                    break
                if attempt == 3:
                    ctx.log(f"Failed to set {key} after 3 attempts ({resp.status_code})")
                time.sleep(5)
            except requests.RequestException as e:
                if attempt == 3:
                    ctx.log(f"Request error setting {key}: {e}")
                time.sleep(5)


def _create_libraries(token: str, ctx: HookContext):
    existing_paths = _get_existing_paths(token)

    for lib in LIBRARIES:
        if lib["location"] in existing_paths:
            ctx.log(f"Skipping {lib['name']} — library already exists at {lib['location']}")
            continue

        for attempt in range(1, 6):
            try:
                resp = requests.post(
                    f"{PLEX_URL}/library/sections",
                    params={
                        "X-Plex-Token": token,
                        "name": lib["name"],
                        "type": lib["type"],
                        "agent": lib["agent"],
                        "scanner": lib["scanner"],
                        "language": "en-US",
                        "location": lib["location"],
                    },
                    timeout=10,
                )
                if resp.ok:
                    ctx.log(f"Created library: {lib['name']}")
                    break
                ctx.log(f"Library creation failed ({resp.status_code}), retrying...")
                time.sleep(5)
            except requests.RequestException:
                time.sleep(5)


def _get_existing_paths(token: str):
    try:
        resp = requests.get(
            f"{PLEX_URL}/library/sections",
            params={"X-Plex-Token": token},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        sections = resp.json().get("MediaContainer", {}).get("Directory", [])
        paths = set()
        for sec in sections:
            for loc in sec.get("Location", []):
                paths.add(loc.get("path", ""))
        return paths
    except requests.RequestException:
        return set()


if __name__ == "__main__":
    HookContext.run()
