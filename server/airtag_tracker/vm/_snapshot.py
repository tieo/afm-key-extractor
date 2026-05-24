"""QEMU internal snapshot management — the debug-iteration unlock.

Wraps the QEMU monitor's ``savevm`` / ``loadvm`` / ``info snapshots`` /
``delvm`` commands so the engine and debug CLI can checkpoint VM state
and replay handlers without re-running 90 minutes of install.

Constraint: internal snapshots store data inside qcow2 disks.  All
*writable* attached disks must therefore be qcow2.  ``MacHDD`` and
``OpenCoreBoot`` are qcow2; ``InstallMedia`` (raw BaseSystem image) is
writeable raw so QEMU cannot store snapshot data in it — savevm during
install fails with "Device 'InstallMedia' is writable but does not
support snapshots".  failure_capture handles this gracefully (best-effort).

Snapshots persist across the *current* QEMU process only when written
to MacHDD's qcow2 file.  Container restart preserves them; ``vm.stop``
followed by ``vm.start`` does too — they are tied to the disk image,
not the QEMU process.
"""

from __future__ import annotations

import re
import time

from .. import qmp
from ..events import emit
from ._lifecycle import VmError, is_running


def _ensure_running() -> None:
    if not is_running():
        raise VmError("VM is not running — start it before taking a snapshot")


def save(label: str, *, deadline_s: float = 60.0) -> dict:
    """Snapshot the running VM under *label* (in-place into the qcow2 disks).

    Blocks until QEMU's monitor returns the prompt, capped at *deadline_s*.
    Typical savevm takes 2-10 seconds depending on guest RAM size (8 GB → ~5 s).

    Returns a dict with keys ``label`` and ``elapsed_s``.  Raises VmError on
    HMP-level failure.
    """
    _ensure_running()
    if not _is_valid_label(label):
        raise VmError(f"Invalid snapshot label {label!r} — use [A-Za-z0-9_-]+")
    t0 = time.monotonic()
    emit("info", "snapshot", f"Saving snapshot: {label}")
    out = qmp.hmp(f"savevm {label}", read_timeout_s=deadline_s)
    elapsed = round(time.monotonic() - t0, 2)
    if _looks_like_error(out):
        raise VmError(f"savevm {label!r} failed: {out.strip()[:300]}")
    emit("info", "snapshot", f"Saved snapshot {label} in {elapsed}s")
    return {"label": label, "elapsed_s": elapsed}


def load(label: str, *, deadline_s: float = 60.0) -> dict:
    """Restore the running VM to *label* (instantaneous from the guest's view)."""
    _ensure_running()
    if not _is_valid_label(label):
        raise VmError(f"Invalid snapshot label {label!r}")
    t0 = time.monotonic()
    emit("info", "snapshot", f"Loading snapshot: {label}")
    out = qmp.hmp(f"loadvm {label}", read_timeout_s=deadline_s)
    elapsed = round(time.monotonic() - t0, 2)
    if _looks_like_error(out):
        raise VmError(f"loadvm {label!r} failed: {out.strip()[:300]}")
    emit("info", "snapshot", f"Loaded snapshot {label} in {elapsed}s")
    return {"label": label, "elapsed_s": elapsed}


def delete(label: str) -> dict:
    """Remove *label* from the qcow2 disks.  No-op if absent."""
    _ensure_running()
    if not _is_valid_label(label):
        raise VmError(f"Invalid snapshot label {label!r}")
    emit("info", "snapshot", f"Deleting snapshot: {label}")
    out = qmp.hmp(f"delvm {label}")
    if _looks_like_error(out) and "no such snapshot" not in out.lower():
        raise VmError(f"delvm {label!r} failed: {out.strip()[:300]}")
    return {"label": label, "deleted": True}


def list_all() -> list[dict]:
    """Return parsed metadata for every snapshot currently stored in the VM disks.

    Each entry is ``{tag, vm_size, date, vm_clock}``.  ``info snapshots``
    output is whitespace-aligned and not officially stable — parser is
    lenient and ignores lines that don't look like snapshot rows.
    """
    _ensure_running()
    out = qmp.hmp("info snapshots")
    return _parse_info_snapshots(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _is_valid_label(label: str) -> bool:
    """Reject labels that could escape HMP word boundaries."""
    return bool(_LABEL_RE.match(label)) and 1 <= len(label) <= 64


def _looks_like_error(hmp_output: str) -> bool:
    """Heuristic: QEMU HMP error responses include 'Error', start with 'qemu',
    or begin with the qemu binary name (e.g. ``qemu-system-x86_64: ...``)."""
    low = hmp_output.lower().lstrip()
    if "error" in low and "no errors" not in low:
        return True
    if low.startswith("qemu"):
        return True
    return False


def _parse_info_snapshots(text: str) -> list[dict]:
    """Best-effort parser for `info snapshots` output.

    Format example::

        List of snapshots present on all disks:
        ID        TAG               VM SIZE                DATE       VM CLOCK     ICOUNT
        1         pre_sa_create     567 MiB 2026-05-20 14:23:01   01:23:45.6
    """
    rows: list[dict] = []
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("ID") and "TAG" in line:
            started = True
            continue
        if not started:
            continue
        # Skip the header separator line if present (e.g. all dashes).
        if set(line) <= set("- "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        # First column is ID (numeric or "--" when the qcow2 has no internal
        # ID assigned).  Second column is TAG — the human-friendly name.
        rows.append({
            "id": parts[0],
            "tag": parts[1],
            "raw": line,
        })
    return rows
