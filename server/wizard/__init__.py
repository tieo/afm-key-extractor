"""Setup Assistant bypass for the AirTag tracker's macOS VM.

Public entry point:

    bypass_setup_assistant(vm, reporter, path="recovery") -> Outcome

where ``vm`` is a :class:`~server.wizard.qemu.VMDriver` and ``reporter``
is a :class:`~server.wizard.reporter.Reporter`.  The two parameters make
the whole pipeline injectable; tests pass fakes, production wires in
the real QEMU monitor + the tracker's ``emit`` / ``_set_phase``.

See ``docs/WIZARD_AUTOMATION.md`` for the design that this module
implements.  The Recovery path (``recovery.py``) is primary;
``gui.py`` is reserved for the explicit operator-requested fallback
described in the design doc but is not yet implemented — calling it
raises :class:`NotImplementedError`, matching the "GUI fallback is
off by default" position of §4.5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .recovery import run_recovery_bypass
from .reporter import Reporter

Path = Literal["recovery", "gui"]
Status = Literal["ok", "failed"]


@dataclass(frozen=True)
class Outcome:
    path: Path
    status: Status
    phase: str
    message: str


def bypass_setup_assistant(vm, reporter: Reporter, path: Path = "recovery",
                           *, sleep=None, now=None) -> Outcome:
    """Run the chosen bypass path.  Never raises; all failure is in the
    returned :class:`Outcome`.  ``sleep`` and ``now`` are test hooks;
    production callers omit them and get real ``time.sleep`` / ``time.time``.
    """
    if path == "recovery":
        return run_recovery_bypass(vm, reporter, sleep=sleep, now=now)
    if path == "gui":
        raise NotImplementedError(
            "GUI fallback is reserved for operator-requested use; "
            "see docs/WIZARD_AUTOMATION.md §5."
        )
    return Outcome(path=path, status="failed", phase="error",
                   message=f"unknown bypass path {path!r}")


__all__ = ["Outcome", "Reporter", "bypass_setup_assistant"]
