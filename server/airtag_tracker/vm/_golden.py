"""Golden disk image bake / restore."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import VM_ENABLED
from ..events import emit
from ._lifecycle import VmError, is_running
from ._qemu import MAC_HDD


def reset_to_golden(golden_path: Path) -> dict:
    """Overwrite ``mac_hdd_ng.img`` with *golden_path* (destructive).

    *golden_path* is required — pass `ctx.adapter.golden_image_path(VM_DIR)`.
    """
    if not VM_ENABLED:
        raise VmError("VM not enabled")
    if is_running():
        raise VmError("VM still running — stop it first")
    if not golden_path.exists():
        raise VmError(f"No golden image to restore from: {golden_path}")
    emit("info", "vm", f"Resetting {MAC_HDD.name} from {golden_path.name}")
    shutil.copy2(golden_path, MAC_HDD)
    return {"status": "reset", "path": str(MAC_HDD)}


def bake_golden(golden_path: Path) -> dict:
    """Snapshot mac_hdd_ng.img → *golden_path* (VM must be stopped).

    *golden_path* is required — pass `ctx.adapter.golden_image_path(VM_DIR)`.
    """
    if not VM_ENABLED:
        raise VmError("VM not enabled")
    if is_running():
        raise VmError("VM still running — stop it first")
    if not MAC_HDD.exists():
        raise VmError("mac_hdd_ng.img not found")

    if golden_path.exists():
        backup = golden_path.with_suffix(golden_path.suffix + ".bak")
        emit("info", "vm", f"Existing golden image backed up to {backup.name}")
        shutil.move(str(golden_path), str(backup))

    emit("info", "vm", f"Baking golden image: {MAC_HDD.name} → {golden_path.name}")
    shutil.copy2(MAC_HDD, golden_path)
    size_gb = golden_path.stat().st_size / (1024 ** 3)
    emit("info", "vm", f"Golden image baked ({size_gb:.1f} GB)")
    return {"status": "baked", "path": str(golden_path), "size_gb": round(size_gb, 2)}
