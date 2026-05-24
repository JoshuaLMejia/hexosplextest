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
    token = _wait_for_token(ctx)
    if not token:
        raise RuntimeError("PlexOnlineToken not found in Preferences.xml.")

    _wait_for_ready(ctx)
    _set_preferences(token, ctx)
    _create_libraries(token, ctx)
    ctx.log("Setup complete.")


def _wait_for_token(ctx: HookContext) -> str:
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
            ctx.log("Waiting for PlexOnlineToken in Preferences.xml...")
        time.sleep(5)

    return ""


def _wait_for_ready(ctx: HookContext):
    ctx.log("Waiting for Plex API...")
    interval = 5.0
    for attempt in range(1, 40):
        try:
            resp = requests.get(
                f"{PLEX_URL}/identity",
                headers={"Accept": "application/json"},
                timeout=5,
            )
            if resp.status_code == 200:
                machine_id = resp.json().get("MediaContainer", {}).get("machineIdentifier", "")
                if machine_id and machine_id != "setup":
                    ctx.log("Plex is ready.")
                    return
        except requests.RequestException:
            pass

        time.sleep(interval)
        interval = min(interval + 2.0, 15.0)

    raise RuntimeError("Plex did not become ready in time.")


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
    # Plex sometimes returns 400 immediately after startup because its internal
    # filesystem scanner hasn't initialized yet. Give it a moment before first attempt.
    time.sleep(5)

    for lib in LIBRARIES:
        existing_paths = _get_existing_paths(token)
        if lib["location"] in existing_paths:
            ctx.log(f"Library already exists: {lib['name']}")
            continue

        created = False
        backoff = 5.0
        max_attempts = 10
        for attempt in range(1, max_attempts + 1):
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
                    timeout=15,
                )
                if resp.ok:
                    # Verify the library actually exists before declaring success
                    if lib["location"] in _get_existing_paths(token):
                        ctx.log(f"Created library: {lib['name']}")
                        created = True
                        break
                    ctx.log(f"{lib['name']} returned 200 but not visible yet, waiting...")
                else:
                    ctx.log(f"{lib['name']} attempt {attempt} failed ({resp.status_code}), retrying in {int(backoff)}s")
            except requests.RequestException as e:
                ctx.log(f"{lib['name']} attempt {attempt} network error: {e}")

            time.sleep(backoff)
            backoff = min(backoff * 1.5, 20.0)

        if not created:
            ctx.log(f"FAILED to create library {lib['name']} after {max_attempts} attempts")


def _get_existing_paths(token: str) -> set:
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
