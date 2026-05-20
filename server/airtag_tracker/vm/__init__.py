"""macOS QEMU VM management.

Re-exports the public surface from the underscore submodules so callers can
keep using ``from airtag_tracker import vm; vm.start()`` without caring
about the internal split.

Submodules:
- ``_qemu``     — binary discovery + arg builders
- ``_lifecycle``— start / stop / is_running / status
- ``_golden``   — bake / reset

The state machine in ``automation/`` is responsible for OpenCore picker
selection, login, and Setup Assistant — this package only manages QEMU
itself.
"""

from __future__ import annotations

# Public exports — keep these stable across the split.
from . import _snapshot as snapshot
from ._golden import bake_golden, reset_to_golden
from ._lifecycle import (
    VmError,
    is_running,
    start,
    start_for_install,
    status,
    stop,
)
from ._qemu import MAC_HDD

# Older references — some handler modules import VM_DIR from `vm` directly
# rather than from config.  Keep the alias so we don't break them.
from ..config import VM_DIR

__all__ = [
    "MAC_HDD",
    "VM_DIR",
    "VmError",
    "bake_golden",
    "is_running",
    "reset_to_golden",
    "snapshot",
    "start",
    "start_for_install",
    "status",
    "stop",
]
