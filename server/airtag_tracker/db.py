"""SQLite location history store."""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime

from .config import DB_PATH


def init() -> None:
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
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_airtag_time ON locations (airtag_id, timestamp)"
    )
    db.commit()
    db.close()


def save_reports(airtag_id: str, airtag_name: str, reports) -> None:
    db = sqlite3.connect(str(DB_PATH))
    now = datetime.now(UTC).isoformat()
    for r in reports:
        db.execute(
            "INSERT INTO locations "
            "(airtag_id, airtag_name, latitude, longitude, accuracy, timestamp, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (airtag_id, airtag_name, r.latitude, r.longitude,
             r.horizontal_accuracy, r.timestamp.isoformat(), now),
        )
    db.commit()
    db.close()


def latest_per_airtag() -> list[dict]:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT airtag_id, airtag_name, latitude, longitude, accuracy, timestamp
        FROM locations
        WHERE id IN (SELECT MAX(id) FROM locations GROUP BY airtag_id)
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def history(airtag_id: str, since: str, limit: int) -> list[dict]:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT latitude, longitude, accuracy, timestamp FROM locations "
        "WHERE airtag_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
        (airtag_id, since, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    R = 6371000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
