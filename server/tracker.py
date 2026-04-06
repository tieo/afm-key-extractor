"""AirTag tracker server — polls Apple's Find My network and serves location history."""

import json
import sqlite3
import time
import threading
import os
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from findmy import FindMyAccessory, AppleAccount, LocalAnisetteProvider
from findmy.reports import LoginState, SyncSmsSecondFactor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

DATA_DIR = Path(os.environ.get("AIRTAG_DATA_DIR", "/var/lib/airtag-tracker"))
KEYS_DIR = DATA_DIR / "keys"
DB_PATH = DATA_DIR / "locations.db"
ACCOUNT_PATH = DATA_DIR / "account.json"
ANISETTE_PATH = DATA_DIR / "ani_libs.bin"
POLL_INTERVAL = int(os.environ.get("AIRTAG_POLL_INTERVAL", "900"))  # 15 min default
PORT = int(os.environ.get("AIRTAG_PORT", "8042"))
STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR))

# In-memory state for login flow
_pending_account = None
_pending_2fa_methods = None


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            airtag_id TEXT NOT NULL,
            airtag_name TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            accuracy INTEGER,
            timestamp TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_locations_airtag_time
        ON locations (airtag_id, timestamp)
    """)
    db.commit()
    db.close()


def load_airtags():
    """Load all AirTag key files from the keys directory."""
    tags = []
    if not KEYS_DIR.exists():
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        return tags
    for f in KEYS_DIR.glob("*.json"):
        try:
            tag = FindMyAccessory.from_json(str(f))
            tags.append((f.stem, tag))
            log.info(f"Loaded AirTag: {f.stem}")
        except Exception as e:
            log.error(f"Failed to load {f}: {e}")
    return tags


def get_account():
    """Get or create an authenticated Apple account."""
    ani = LocalAnisetteProvider(libs_path=str(ANISETTE_PATH))
    if ACCOUNT_PATH.exists():
        try:
            acc = AppleAccount.from_json(str(ACCOUNT_PATH), anisette=ani)
            log.info("Restored Apple account session")
            return acc
        except Exception as e:
            log.warning(f"Failed to restore session: {e}")
    return None


def save_locations(airtag_id, airtag_name, reports):
    """Save location reports to the database."""
    db = sqlite3.connect(str(DB_PATH))
    now = datetime.now(timezone.utc).isoformat()
    for report in reports:
        db.execute(
            "INSERT INTO locations (airtag_id, airtag_name, latitude, longitude, accuracy, timestamp, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (airtag_id, airtag_name, report.latitude, report.longitude,
             report.horizontal_accuracy, report.timestamp.isoformat(), now),
        )
    db.commit()
    db.close()


def poll_locations():
    """Fetch latest locations for all AirTags."""
    acc = get_account()
    if not acc:
        log.warning("No Apple account configured, skipping poll")
        return

    tags = load_airtags()
    if not tags:
        log.info("No AirTags configured, skipping poll")
        return

    try:
        accessories = [tag for _, tag in tags]
        history = acc.fetch_location_history(accessories)

        for (tag_id, tag), reports in zip(tags, [history.get(t, []) for t in accessories]):
            if reports:
                save_locations(tag_id, getattr(tag, "name", tag_id), reports)
                log.info(f"Saved {len(reports)} reports for {tag_id}")
            else:
                log.info(f"No new reports for {tag_id}")

        # Save updated account state and tag alignment
        acc.to_json(str(ACCOUNT_PATH))
        for (tag_id, tag) in tags:
            tag.to_json(str(KEYS_DIR / f"{tag_id}.json"))

    except Exception as e:
        log.error(f"Poll failed: {e}", exc_info=True)


def poll_loop():
    """Background thread that polls on an interval."""
    while True:
        try:
            poll_locations()
        except Exception as e:
            log.error(f"Poll loop error: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


# --- API routes ---

@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/api/airtags")
def list_airtags():
    """List all known AirTags with their latest position."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT airtag_id, airtag_name, latitude, longitude, accuracy, timestamp
        FROM locations
        WHERE id IN (
            SELECT MAX(id) FROM locations GROUP BY airtag_id
        )
    """).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/airtags/<airtag_id>/history")
def airtag_history(airtag_id):
    """Get location history for a specific AirTag."""
    since = request.args.get("since", "1970-01-01T00:00:00")
    limit = int(request.args.get("limit", "1000"))
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT latitude, longitude, accuracy, timestamp FROM locations "
        "WHERE airtag_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
        (airtag_id, since, limit),
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/poll", methods=["POST"])
def trigger_poll():
    """Manually trigger a location poll."""
    threading.Thread(target=poll_locations, daemon=True).start()
    return jsonify({"status": "polling"})


@app.route("/api/extract-keys", methods=["POST"])
def trigger_extract():
    """Trigger macOS VM to extract AirTag keys."""
    import subprocess as sp
    try:
        result = sp.run(
            ["systemctl", "start", "airtag-extract-keys"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return jsonify({"status": "error", "message": result.stderr.strip()}), 500
        return jsonify({"status": "started", "message": "Key extraction VM started. This takes a few minutes."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/account/status")
def account_status():
    """Check if Apple account is configured and session is valid."""
    return jsonify({
        "configured": ACCOUNT_PATH.exists(),
        "airtags": len(list(KEYS_DIR.glob("*.json"))) if KEYS_DIR.exists() else 0,
    })


@app.route("/api/account/login", methods=["POST"])
def account_login():
    """Start Apple ID login. Returns 2FA methods if needed."""
    global _pending_account, _pending_2fa_methods
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"error": "email and password required"}), 400

    try:
        ani = LocalAnisetteProvider(libs_path=str(ANISETTE_PATH))
        acc = AppleAccount(ani)
        state = acc.login(email, password)

        if state == LoginState.REQUIRE_2FA:
            _pending_account = acc
            methods = acc.get_2fa_methods()
            _pending_2fa_methods = methods
            # Auto-request the first method
            if methods:
                methods[0].request()
            method_list = []
            for m in methods:
                if isinstance(m, SyncSmsSecondFactor):
                    method_list.append({"type": "sms", "phone": m.phone_number, "id": m.phone_number_id})
                else:
                    method_list.append({"type": "trusted_device"})
            return jsonify({"status": "2fa_required", "methods": method_list})

        if state == LoginState.LOGGED_IN:
            acc.to_json(str(ACCOUNT_PATH))
            _pending_account = None
            _pending_2fa_methods = None
            return jsonify({"status": "logged_in"})

        return jsonify({"error": f"Unexpected login state: {state}"}), 500
    except Exception as e:
        _pending_account = None
        _pending_2fa_methods = None
        return jsonify({"error": str(e)}), 500


@app.route("/api/account/2fa", methods=["POST"])
def account_2fa():
    """Submit 2FA code to complete login."""
    global _pending_account, _pending_2fa_methods
    if not _pending_account or not _pending_2fa_methods:
        return jsonify({"error": "No pending login. Call /api/account/login first."}), 400

    data = request.get_json()
    code = data.get("code")
    method_index = data.get("method", 0)

    if not code:
        return jsonify({"error": "code required"}), 400

    try:
        method = _pending_2fa_methods[method_index]
        state = method.submit(code)

        if state == LoginState.LOGGED_IN:
            _pending_account.to_json(str(ACCOUNT_PATH))
            _pending_account = None
            _pending_2fa_methods = None
            return jsonify({"status": "logged_in"})

        return jsonify({"error": f"2FA failed, state: {state}"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/account/2fa/request", methods=["POST"])
def account_2fa_request():
    """Request a 2FA code to be sent (for SMS methods)."""
    global _pending_2fa_methods
    if not _pending_2fa_methods:
        return jsonify({"error": "No pending login."}), 400

    data = request.get_json() or {}
    method_index = data.get("method", 0)

    try:
        method = _pending_2fa_methods[method_index]
        method.request()
        return jsonify({"status": "sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys/upload", methods=["POST"])
def upload_keys():
    """Upload AirTag key files (JSON format from FindMy.py or plist_to_findmy.py)."""
    if "file" in request.files:
        f = request.files["file"]
        if not f.filename.endswith(".json"):
            return jsonify({"error": "Only .json files accepted"}), 400
        try:
            data = json.loads(f.read())
            # Validate it's a valid key file
            FindMyAccessory.from_json(data)
            name = data.get("name", f.filename.rsplit(".", 1)[0])
            safe_name = name.replace(" ", "_").replace("/", "_")
            out_path = KEYS_DIR / f"{safe_name}.json"
            with open(out_path, "w") as out:
                json.dump(data, out, indent=2)
            return jsonify({"status": "ok", "name": safe_name})
        except Exception as e:
            return jsonify({"error": f"Invalid key file: {e}"}), 400
    elif request.is_json:
        data = request.get_json()
        try:
            FindMyAccessory.from_json(data)
            name = data.get("name", data.get("identifier", "unknown"))
            safe_name = name.replace(" ", "_").replace("/", "_")
            out_path = KEYS_DIR / f"{safe_name}.json"
            with open(out_path, "w") as out:
                json.dump(data, out, indent=2)
            return jsonify({"status": "ok", "name": safe_name})
        except Exception as e:
            return jsonify({"error": f"Invalid key data: {e}"}), 400
    return jsonify({"error": "Send a JSON file or JSON body"}), 400


@app.route("/api/keys", methods=["GET"])
def list_keys():
    """List all loaded AirTag key files."""
    keys = []
    if KEYS_DIR.exists():
        for f in KEYS_DIR.glob("*.json"):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                keys.append({
                    "file": f.name,
                    "name": data.get("name", f.stem),
                    "model": data.get("model", "unknown"),
                    "identifier": data.get("identifier", ""),
                })
            except Exception:
                keys.append({"file": f.name, "name": f.stem, "error": True})
    return jsonify(keys)


@app.route("/api/keys/<name>", methods=["DELETE"])
def delete_key(name):
    """Delete an AirTag key file."""
    path = KEYS_DIR / f"{name}.json"
    if not path.exists():
        return jsonify({"error": "Key not found"}), 404
    path.unlink()
    return jsonify({"status": "deleted"})


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    # Start background poller
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()

    log.info(f"Starting tracker on port {PORT}")
    app.run(host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
