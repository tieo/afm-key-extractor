"""Boot-phase handlers for the runtime automation flow.

Covers three states:
- RESTORING_GOLDEN  → copy golden HDD image to working image
- BOOTING           → start the QEMU VM (noVNC is started by vm.start())
- PICKER_SELECTING  → wait for the OpenCore picker and select macOS
"""

from __future__ import annotations

import shutil
import time

from ... import vm
from ...events import emit
from ..context import AutomationContext
from ..states import RuntimeState
from .. import screen
from ..install.opencore import select_macos_entry


def restore_golden(ctx: AutomationContext) -> RuntimeState:
    """Copy the golden HDD image to the working MAC_HDD path.

    If ``ctx.restore_golden`` is False the copy is skipped and we
    proceed directly to booting — useful when re-running the flow on an
    already-customised image without wanting to lose VM state.

    Raises RuntimeError if restore_golden is True but the golden image
    does not exist.
    """
    if not ctx.restore_golden:
        emit("info", "boot", "restore_golden=False — skipping image copy")
        return RuntimeState.BOOTING

    if not vm.GOLDEN_HDD.exists():
        raise RuntimeError(
            f"Golden HDD image not found at {vm.GOLDEN_HDD}. "
            "Run the installation flow first."
        )

    emit("info", "boot", f"Restoring golden image: {vm.GOLDEN_HDD} → {vm.MAC_HDD}")
    shutil.copy2(vm.GOLDEN_HDD, vm.MAC_HDD)
    emit("info", "boot", "Golden image restored")
    return RuntimeState.BOOTING


def start_vm(ctx: AutomationContext) -> RuntimeState:
    """Start the QEMU VM in automation mode (no autotyper, no blind boot picks).

    Passes automation=True to vm.start() so the login autotyper and the
    blind OpenCore key-mash are suppressed — the state machine handles
    both via OCR-based detection.
    """
    emit("info", "boot", "Starting VM (automation mode)")
    vm.start(automation=True)
    emit("info", "boot", "VM started — waiting for OpenCore picker")
    return RuntimeState.PICKER_SELECTING


def select_macos(ctx: AutomationContext) -> RuntimeState:
    """Wait for the OpenCore boot picker and select the macOS entry.

    Uses OCR-based picker navigation (same as the install flow) to detect the
    macOS entry position dynamically rather than relying on a hardcoded key
    sequence that breaks when the picker entry order changes.

    Polls every 5 s for up to 90 s.  Raises RuntimeError on timeout.
    """
    deadline_s = 90
    poll_s = 5.0
    progress_interval_s = 20
    t0 = time.time()
    last_progress = t0
    emit("info", "boot", "Waiting for OpenCore picker (up to 90 s)")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            emit("info", "boot",
                 f"Still waiting for OpenCore picker… ({elapsed:.0f}s)")
            last_progress = now
        if screen.detect_opencore_picker():
            emit("info", "boot", "OpenCore picker detected — selecting macOS")
            select_macos_entry(ctx)
            return RuntimeState.WAITING_LOGIN_SCREEN
        time.sleep(poll_s)
    raise RuntimeError(f"OpenCore picker not detected within {deadline_s}s")
