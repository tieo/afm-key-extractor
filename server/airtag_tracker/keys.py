"""AirTag key file management."""

from __future__ import annotations

import json
from pathlib import Path

from findmy import FindMyAccessory

from .config import KEYS_DIR
from .events import emit


def _safe_name(name: str) -> str:
    return name.replace(" ", "_").replace("/", "_")


def load_all() -> list[tuple[str, FindMyAccessory]]:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in KEYS_DIR.glob("*.json"):
        try:
            tag = FindMyAccessory.from_json(str(f))
            out.append((f.stem, tag))
            emit("info", "keys", f"Loaded AirTag: {f.stem}")
        except Exception as e:
            emit("error", "keys", f"Failed to load {f.name}: {e}")
    return out


def list_metadata() -> list[dict]:
    out = []
    if not KEYS_DIR.exists():
        return out
    for f in KEYS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            out.append({
                "file": f.name,
                "name": data.get("name", f.stem),
                "model": data.get("model", "unknown"),
                "identifier": data.get("identifier", ""),
            })
        except Exception:
            out.append({"file": f.name, "name": f.stem, "error": True})
    return out


def count() -> int:
    return len(list(KEYS_DIR.glob("*.json"))) if KEYS_DIR.exists() else 0


def save_upload(data: dict, fallback_name: str) -> str:
    """Validate key JSON and write to KEYS_DIR. Returns the safe filename stem."""
    FindMyAccessory.from_json(data)
    name = data.get("name") or data.get("identifier") or fallback_name
    stem = _safe_name(name)
    path = KEYS_DIR / f"{stem}.json"
    path.write_text(json.dumps(data, indent=2))
    return stem


def delete(name: str) -> bool:
    path = KEYS_DIR / f"{name}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def path_for(airtag_id: str) -> Path:
    return KEYS_DIR / f"{airtag_id}.json"
