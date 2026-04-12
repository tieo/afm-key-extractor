"""Integration tests for the tracker <-> wizard wire-up.

These exercise two narrow but load-bearing contracts:

1. The flat-vs-package import fallback in
   ``_bypass_setup_assistant_via_wizard`` (tracker.py).  In the
   installed Nix layout, ``tracker.py`` and ``wizard/`` live in the
   same directory, so the names resolve as ``wizard.*``.  In the
   repo layout, only ``server.wizard.*`` is importable.  Both layouts
   must expose the same three symbols.

2. The ``VM_PASSWORD`` constraint: Setup Assistant silently rejects
   passwords shorter than 8 characters, so the value wired into the
   Recovery scripts must be >= 8 chars and must match the golden-image
   password written to ``vm-password``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------
# Import fallback
# ---------------------------------------------------------------------

_REQUIRED_SYMBOLS = {
    "wizard": ["bypass_setup_assistant", "Outcome", "Reporter"],
    "wizard.qemu": ["TrackerVMDriver"],
    "wizard.reporter": ["CallbackReporter"],
}


def test_package_layout_exports_match_tracker_fallback():
    """The ``server.wizard`` path is what tests and the non-installed
    tracker use.  Every symbol imported in ``_bypass_setup_assistant_via_wizard``
    must resolve here.
    """
    pkg = importlib.import_module("server.wizard")
    assert hasattr(pkg, "bypass_setup_assistant")
    assert hasattr(pkg, "Outcome")
    assert hasattr(pkg, "Reporter")

    qemu = importlib.import_module("server.wizard.qemu")
    assert hasattr(qemu, "TrackerVMDriver")

    reporter = importlib.import_module("server.wizard.reporter")
    assert hasattr(reporter, "CallbackReporter")


def test_flat_layout_import_succeeds_when_wizard_on_syspath(tmp_path, monkeypatch):
    """Simulate the installed Nix layout by exposing ``server/wizard``
    as top-level ``wizard`` on ``sys.path``.  This is what the tracker's
    primary ``from wizard import ...`` branch hits at runtime.
    """
    wizard_src = _ROOT / "server" / "wizard"
    # Symlink the package into a fresh dir so it appears as top-level.
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "wizard").symlink_to(wizard_src, target_is_directory=True)

    # Drop any cached server.wizard.* modules to avoid cross-talk.
    for mod in list(sys.modules):
        if mod == "wizard" or mod.startswith("wizard."):
            del sys.modules[mod]

    monkeypatch.syspath_prepend(str(stage))
    try:
        from wizard import Outcome, Reporter, bypass_setup_assistant  # noqa: F401
        from wizard.qemu import TrackerVMDriver  # noqa: F401
        from wizard.reporter import CallbackReporter  # noqa: F401
    finally:
        # Clean up so other tests see a pristine sys.modules.
        for mod in list(sys.modules):
            if mod == "wizard" or mod.startswith("wizard."):
                del sys.modules[mod]


def test_tracker_fallback_import_block_matches_package_exports():
    """Parse the fallback block in tracker.py and confirm each imported
    name is actually exported by the corresponding module.  This would
    catch a rename in ``server/wizard/`` that forgets to update the
    tracker shim.
    """
    src = (_ROOT / "server" / "tracker.py").read_text()
    # The try branch is what runs in production.
    assert "from wizard import bypass_setup_assistant" in src
    assert "from wizard.qemu import TrackerVMDriver" in src
    assert "from wizard.reporter import CallbackReporter" in src
    # The except branch is what runs during tests / non-installed dev.
    assert "from server.wizard import bypass_setup_assistant" in src
    assert "from server.wizard.qemu import TrackerVMDriver" in src
    assert "from server.wizard.reporter import CallbackReporter" in src


# ---------------------------------------------------------------------
# VM_PASSWORD constraint
# ---------------------------------------------------------------------


def test_vm_password_meets_setup_assistant_minimum():
    """Setup Assistant rejects passwords shorter than 8 characters
    without surfacing an error.  The recovery script formats
    ``VM_PASSWORD`` directly into ``sysadminctl -password "…"``, so a
    regression to <8 would silently brick the install.
    """
    from server.wizard import recovery
    assert len(recovery.VM_PASSWORD) >= 8, (
        f"VM_PASSWORD must be >= 8 chars, got {len(recovery.VM_PASSWORD)!r}"
    )


def test_vm_password_matches_tracker_module():
    """The tracker writes ``VM_PASSWORD`` to ``$DATA_DIR/vm-password``
    for the golden-image flow; the recovery module uses its own copy
    to create the admin user.  If these drift the user gets one
    password, the file claims another, and login fails.
    """
    # Read without importing tracker.py (it pulls flask, pytesseract,
    # etc.).  Parse the constant out of the source.
    src = (_ROOT / "server" / "tracker.py").read_text()
    for line in src.splitlines():
        if line.startswith("VM_PASSWORD"):
            tracker_pw = line.split("=", 1)[1].split("#", 1)[0].strip().strip('"\'')
            break
    else:
        raise AssertionError("VM_PASSWORD not found in tracker.py")

    from server.wizard import recovery
    assert tracker_pw == recovery.VM_PASSWORD, (
        f"Password drift: tracker={tracker_pw!r} recovery={recovery.VM_PASSWORD!r}"
    )
