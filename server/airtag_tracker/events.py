"""In-memory event log with stdout logging."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

log = logging.getLogger("tracker")

MAX_EVENTS = 500
_event_log: list[dict] = []
_lock = threading.Lock()


def emit(level: str, category: str, message: str) -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "level": level,
        "cat": category,
        "msg": message,
    }
    with _lock:
        _event_log.append(entry)
        if len(_event_log) > MAX_EVENTS:
            del _event_log[: len(_event_log) - MAX_EVENTS]
    getattr(log, level, log.info)(f"[{category}] {message}")


def snapshot() -> list[dict]:
    with _lock:
        return list(_event_log)
