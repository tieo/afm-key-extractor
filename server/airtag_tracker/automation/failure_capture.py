"""Automatic post-mortem artifacts on engine failure.

When a handler raises or exhausts its retry budget, the engine calls
``capture(state, error)`` here.  It best-effort writes a snapshot of
everything you'd need to investigate the failure later:

- ``screen.png`` — what was on the framebuffer at the moment of failure
- ``log.txt``   — last events.snapshot() lines
- ``meta.json`` — state name, error message, timestamp, snapshot label
                  (if a savevm snapshot was also written)
- The VM is auto-snapshotted under ``fail_<state>_<ts>`` so the harness
  can `loadvm` back to the failure state without reproducing the run.

Artifact retention is bounded (``MAX_KEPT``) so failed runs don't fill the
disk — older directories are pruned on every capture.

Failure capture itself is best-effort: any sub-step that raises is logged
and the rest continues.  We never let the failure-capture path mask the
real engine error.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ..config import DATA_DIR
from ..events import emit

FAILURE_DIR = DATA_DIR / "failures"
MAX_KEPT = 5  # rotate: keep the last N failure directories


def capture(state_value: str, error: str) -> dict:
    """Write screen/log/meta/snapshot artifacts for a failed state.

    Returns a dict describing what was successfully captured (paths, snapshot
    label if any).  Never raises — best-effort.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_state = "".join(c if c.isalnum() or c in "_-" else "_" for c in state_value)
    dir_name = f"{safe_state}_{ts}"
    out = FAILURE_DIR / dir_name

    result: dict = {"dir": str(out), "state": state_value, "error": error, "ts": ts}

    try:
        FAILURE_DIR.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        emit("warning", "failure_capture", f"could not create {out}: {e}")
        return result

    _try_screen(out, result)
    _try_log(out, result)
    _try_snapshot(safe_state, ts, result)
    _write_meta(out, result)
    _rotate()

    emit("info", "failure_capture",
         f"Failure artifacts saved to {out}"
         + (f" (snapshot {result['snapshot']})" if result.get("snapshot") else ""))
    return result


def _try_screen(out: Path, result: dict) -> None:
    """Save current framebuffer as PNG."""
    try:
        from .. import vm_ui
        ppm = vm_ui._screendump()
    except Exception as e:
        emit("warning", "failure_capture", f"screendump failed: {e}")
        return
    png_path = out / "screen.png"
    try:
        from PIL import Image
        with Image.open(ppm) as im:
            im.convert("RGB").save(png_path)
        result["screen"] = str(png_path)
    except Exception as e:
        emit("warning", "failure_capture", f"PNG convert failed: {e}")
    finally:
        try:
            Path(ppm).unlink(missing_ok=True)
        except Exception:
            pass


def _try_log(out: Path, result: dict) -> None:
    """Dump the last events.snapshot() entries."""
    try:
        from .. import events as events_mod
        entries = events_mod.snapshot()
    except Exception as e:
        emit("warning", "failure_capture", f"log snapshot failed: {e}")
        return
    path = out / "log.txt"
    try:
        with path.open("w") as f:
            for entry in entries:
                # entry keys: ts, level, cat, msg
                ts = entry.get("ts", "")
                lvl = entry.get("level", "").upper()
                cat = entry.get("cat", "")
                msg = entry.get("msg", "")
                f.write(f"{ts} {lvl:<7} [{cat}] {msg}\n")
        result["log"] = str(path)
    except Exception as e:
        emit("warning", "failure_capture", f"log write failed: {e}")


def _try_snapshot(safe_state: str, ts: str, result: dict) -> None:
    """Best-effort QEMU savevm tagged ``fail_<state>_<ts>``.

    Skipped if the VM is not running.  Failures here are common (e.g. VM
    crashed) and must not mask the engine error.
    """
    try:
        from .. import vm
    except Exception:
        return
    if not vm.is_running():
        return
    # Snapshot labels accept [A-Za-z0-9_-]+ and are ≤64 chars.
    label = f"fail_{safe_state}_{ts}"[:64]
    try:
        vm.snapshot.save(label, deadline_s=30.0)
        result["snapshot"] = label
    except Exception as e:
        emit("warning", "failure_capture", f"savevm {label!r} failed: {e}")


def _write_meta(out: Path, result: dict) -> None:
    try:
        (out / "meta.json").write_text(json.dumps(result, indent=2))
    except Exception as e:
        emit("warning", "failure_capture", f"meta write failed: {e}")


def _rotate() -> None:
    """Keep the MAX_KEPT most-recently-modified failure directories."""
    try:
        if not FAILURE_DIR.exists():
            return
        dirs = sorted(
            (p for p in FAILURE_DIR.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in dirs[MAX_KEPT:]:
            try:
                shutil.rmtree(stale)
            except Exception as e:
                emit("warning", "failure_capture", f"rotate rm {stale} failed: {e}")
    except Exception as e:
        emit("warning", "failure_capture", f"rotate failed: {e}")
