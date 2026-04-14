"""Persisted polling settings + live poll state."""

from __future__ import annotations

import json
import threading

from .config import DEFAULT_SETTINGS, POLL_INTERVAL, SETTINGS_PATH

_lock = threading.Lock()

state = {
    "moving": False,
    "idle_count": 0,
    "last_positions": {},
    "current_interval": POLL_INTERVAL,
    "last_poll": None,
}


def load() -> dict:
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH) as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save(settings: dict) -> None:
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def update(partial: dict) -> dict:
    """Merge and persist allowed keys. Returns the new settings."""
    allowed = set(DEFAULT_SETTINGS.keys())
    settings = load()
    for k in allowed & partial.keys():
        settings[k] = partial[k]
    save(settings)
    with _lock:
        if not state["moving"]:
            state["current_interval"] = settings["idle_interval"]
    return settings


def lock():
    return _lock
