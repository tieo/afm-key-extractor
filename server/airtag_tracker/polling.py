"""Background location poller with adaptive interval."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from . import account_storage, db, keys, settings
from .config import ANISETTE_PATH
from .events import emit


def _get_account():
    if not account_storage.exists():
        return None
    try:
        acc = account_storage.load(str(ANISETTE_PATH))
        if acc is not None:
            emit("info", "account", "Restored Apple account session")
        return acc
    except Exception as e:
        emit("warning", "account", f"Failed to restore session: {e}")
        return None


def poll_once() -> bool:
    """Fetch latest locations for all AirTags. Returns True if any moved."""
    emit("info", "poll", "Starting location poll")
    acc = _get_account()
    if not acc:
        emit("warning", "poll", "No Apple account configured, skipping poll")
        return False

    tags = keys.load_all()
    if not tags:
        emit("info", "poll", "No AirTags configured, skipping poll")
        return False

    cfg = settings.load()
    any_moved = False
    total = 0

    try:
        accessories = [t for _, t in tags]
        emit("info", "poll", f"Querying Apple Find My for {len(accessories)} tag(s)")
        history = acc.fetch_location_history(accessories)

        for (tag_id, tag), reports in zip(
            tags, [history.get(t, []) for t in accessories], strict=False
        ):
            name = getattr(tag, "name", tag_id)
            if not reports:
                emit("info", "poll", f"{name}: no new reports")
                continue
            db.save_reports(tag_id, name, reports)
            total += len(reports)

            latest = max(reports, key=lambda r: r.timestamp)
            prev = settings.state["last_positions"].get(tag_id)
            if prev:
                dist = db.haversine(prev[0], prev[1], latest.latitude, latest.longitude)
                if dist > cfg["movement_threshold"]:
                    any_moved = True
                    emit("info", "movement",
                         f"{name} moved {dist:.0f}m (threshold: {cfg['movement_threshold']}m)")
                else:
                    emit("info", "poll",
                         f"{name}: {len(reports)} report(s), stationary ({dist:.0f}m)")
            else:
                emit("info", "poll",
                     f"{name}: {len(reports)} report(s), first position recorded")
            settings.state["last_positions"][tag_id] = (latest.latitude, latest.longitude)

        account_storage.save(acc)
        for tag_id, tag in tags:
            tag.to_json(str(keys.path_for(tag_id)))

        emit("info", "poll", f"Poll complete: {total} report(s) from {len(tags)} tag(s)")
    except Exception as e:
        emit("error", "poll", f"Poll failed: {e}")

    return any_moved


def _apply_adaptive(moved: bool, cfg: dict) -> None:
    with settings.lock():
        prev_moving = settings.state["moving"]
        if cfg.get("adaptive", True) and moved:
            settings.state["moving"] = True
            settings.state["idle_count"] = 0
            settings.state["current_interval"] = cfg["active_interval"]
            if not prev_moving:
                emit("info", "adaptive",
                     f"Switching to active polling (every {cfg['active_interval']}s)")
        elif cfg.get("adaptive", True):
            settings.state["idle_count"] += 1
            if settings.state["idle_count"] >= cfg["cooldown_polls"]:
                if prev_moving:
                    emit("info", "adaptive",
                         f"No movement for {cfg['cooldown_polls']} polls, "
                         f"returning to idle (every {cfg['idle_interval']}s)")
                settings.state["moving"] = False
                settings.state["current_interval"] = cfg["idle_interval"]
        else:
            settings.state["moving"] = False
            settings.state["current_interval"] = cfg["idle_interval"]


def loop() -> None:
    emit("info", "system", "Poll loop started")
    while True:
        cfg = settings.load()
        try:
            moved = poll_once()
            settings.state["last_poll"] = datetime.now(UTC).isoformat()
            _apply_adaptive(moved, cfg)
        except Exception as e:
            emit("error", "poll", f"Poll loop error: {e}")
        time.sleep(settings.state["current_interval"])


def start_background() -> None:
    threading.Thread(target=loop, daemon=True).start()


def poll_async() -> None:
    threading.Thread(target=poll_once, daemon=True).start()
