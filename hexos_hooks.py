import argparse
import json
import yaml


class HookContext:
    def __init__(self, data: dict):
        self.payload = data
        self.app_id = data.get("appId", "")
        self.event = data.get("event", "")
        self.from_version = data.get("fromVersion")
        self.to_version = data.get("toVersion")
        self.app_config_path = data.get("appConfigPath")
        self.mounts = data.get("mounts", {})
        self.container_name = data.get("containerName", "")
        self.truenas_api_key = data.get("truenasApiKey", "")
        self.truenas_rest_url = data.get("truenasRestUrl", "http://localhost/api/v2.0")
        self._log_callback = data.get("_log_callback")

    def log(self, msg: str):
        if callable(self._log_callback):
            self._log_callback(msg)
        else:
            print(json.dumps({"type": "log", "message": msg}), flush=True)

    def read_yaml(self, path: str):
        with open(path) as f:
            return yaml.safe_load(f)

    def write_yaml(self, path: str, data: dict):
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    @staticmethod
    def run():
        parser = argparse.ArgumentParser()
        parser.add_argument("--entrypoint", required=True)
        parser.add_argument("--context", required=True)
        args = parser.parse_args()
        ctx = HookContext(json.loads(args.context))
        import __main__
        fn = getattr(__main__, args.entrypoint)
        fn(ctx)
