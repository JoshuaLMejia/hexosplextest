import json
import os
from flask import Flask, jsonify

STATUS_FILE = "/tmp/plex-status.json"
PORT = 8765

app = Flask(__name__)


@app.route("/status")
def status():
    try:
        with open(STATUS_FILE) as f:
            return jsonify(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"steps": {}})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
