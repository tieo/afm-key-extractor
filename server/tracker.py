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
    # This will be implemented when we add VM orchestration
    return jsonify({"status": "not_implemented"}), 501


@app.route("/api/account/status")
def account_status():
    """Check if Apple account is configured and session is valid."""
    return jsonify({
        "configured": ACCOUNT_PATH.exists(),
        "airtags": len(list(KEYS_DIR.glob("*.json"))) if KEYS_DIR.exists() else 0,
    })


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
