"""In-memory event log with stdout logging and optional SSE broadcast hook."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Callable

log = logging.getLogger("tracker")

MAX_EVENTS = 500
_event_log: list[dict] = []
_lock = threading.Lock()

# Optional callback registered by the FastAPI app so log entries appear
# in the real-time SSE stream without a circular import.
_broadcast_hook: Callable[[dict], None] | None = None


def set_broadcast_hook(fn: Callable[[dict], None]) -> None:
    global _broadcast_hook
    _broadcast_hook = fn


def emit(level: str, category: str, message: str) -> None:
    entry: dict = {
        "type": "log",
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
    if _broadcast_hook is not None:
        try:
            _broadcast_hook(entry)
        except Exception:
            pass


def snapshot() -> list[dict]:
    with _lock:
        return list(_event_log)
