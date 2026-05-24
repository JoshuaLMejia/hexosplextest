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
    # Preferences.xml was already patched with PlexOnlineToken before Plex started.
    # We just need to wait for Plex to be ready, then create libraries.
    token = _read_token(ctx)
    if not token:
        raise RuntimeError("PlexOnlineToken not found in Preferences.xml.")

    _wait_for_ready(ctx)
    _create_libraries(token, ctx)
    ctx.log("Setup complete.")


def _read_token(ctx: HookContext) -> str:
    """Read PlexOnlineToken directly from Preferences.xml."""
    for attempt in range(1, 21):
        try:
            with open(PREFS_PATH, "r") as f:
                contents = f.read()
            match = re.search(r'PlexOnlineToken="([^"]+)"', contents)
            if match:
                return match.group(1)
        except (FileNotFoundError, IOError):
            pass

        if attempt == 1:
            ctx.log("Waiting for Preferences.xml...")
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
                # Reject our Flask stub — it returns machineIdentifier="setup"
                if machine_id and machine_id != "setup":
                    ctx.log("Plex is ready.")
                    return
        except requests.RequestException:
            pass

        time.sleep(interval)
        interval = min(interval + 2.0, 15.0)

    raise RuntimeError("Plex did not become ready in time.")


def _create_libraries(token: str, ctx: HookContext):
    existing_paths = _get_existing_paths(token)

    for lib in LIBRARIES:
        if lib["location"] in existing_paths:
            ctx.log(f"Library already exists: {lib['name']}")
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
