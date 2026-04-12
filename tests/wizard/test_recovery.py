"""End-to-end tests for the Recovery bypass flow with a fake VMDriver.

These tests exercise the *decision tree* of ``run_recovery_bypass`` —
when does it abort, when does it fall back to dscl, what phases fire
in what order.  They do not touch QEMU.
"""

from __future__ import annotations

import pytest

from server.wizard import bypass_setup_assistant
from server.wizard.reporter import CapturingReporter
from tests.wizard.conftest import FakeClock, FakeVMDriver


def _run(vm, rep, path="recovery"):
    """Helper: always run under the fake clock so timeouts resolve
    deterministically instead of waiting wall-clock seconds."""
    clk = FakeClock()
    return bypass_setup_assistant(vm, rep, path=path, sleep=clk.sleep, now=clk.now)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _sa_keys_sentinels() -> str:
    """The SetupAssistant sentinel block the happy-path expects."""
    return (
        "WIZARD_SENTINEL APPLESETUPDONE_OK\n"
        "WIZARD_SENTINEL SA_PLIST_OK\n"
        "WIZARD_SENTINEL USER_SA_OK\n"
        "WIZARD_SENTINEL KBD_OK\n"
    )


def _happy_ocr_script() -> list[str]:
    return [
        # _find_data_volume
        "WIZARD_SENTINEL DVOL=/Volumes/Macintosh HD - Data\n"
        "WIZARD_SENTINEL DVOL_RW=1\n",
        # _try_sysadminctl
        "WIZARD_SENTINEL USER_CREATED=0\n"
        "WIZARD_SENTINEL USER_PLIST_OK\n"
        "WIZARD_SENTINEL SHADOW_OK\n",
        # _write_setup_sentinels
        _sa_keys_sentinels(),
    ]


def _happy_screens() -> list[str]:
    # enough to cover: boot → recovery, open terminal, post-reboot
    return [
        "recovery",          # recovery boot wait
        "terminal",          # open terminal wait
        "desktop",           # post-reboot wait
    ]


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------

def test_recovery_bypass_happy_path():
    vm = FakeVMDriver(
        screens=_happy_screens(),
        ocr_responses=_happy_ocr_script(),
    )
    rep = CapturingReporter()
    out = _run(vm, rep, path="recovery")

    assert out.status == "ok"
    assert out.phase == "done"
    assert out.path == "recovery"

    phases = rep.phase_names()
    assert phases[:1] == ["recovery_boot"]
    assert "recovery_terminal" in phases
    assert "recovery_create_user" in phases
    assert "recovery_reboot" in phases
    assert phases[-1] == "done"

    # VM was restarted twice: once without MacHDD, once with.
    calls = [c[0] for c in vm.calls]
    assert "restart_without_mac_hdd" in calls
    assert "restart_with_mac_hdd" in calls
    # Order: no-MacHDD first, then with.
    assert calls.index("restart_without_mac_hdd") < calls.index("restart_with_mac_hdd")


# ---------------------------------------------------------------------
# Abort paths — every one of these must NOT silently succeed.
# ---------------------------------------------------------------------

def test_recovery_boot_failure_aborts_before_terminal():
    vm = FakeVMDriver(screens=["unknown"], restart_without_ok=True)
    rep = CapturingReporter()
    out = _run(vm, rep)
    assert out.status == "failed"
    assert out.phase == "recovery_boot"
    # Must not have attempted MacHDD restart
    assert ("restart_with_mac_hdd",) not in vm.calls


def test_restart_no_mac_hdd_failure_aborts():
    vm = FakeVMDriver(restart_without_ok=False)
    rep = CapturingReporter()
    out = _run(vm, rep)
    assert out.status == "failed"
    assert out.phase == "recovery_boot"


def test_missing_dvol_sentinel_aborts_before_writing_plist():
    vm = FakeVMDriver(
        screens=["recovery", "terminal"],
        ocr_responses=[
            # _find_data_volume returns nothing useful
            "no dvol here\n",
        ],
    )
    rep = CapturingReporter()
    out = _run(vm, rep)
    assert out.status == "failed"
    assert out.phase == "recovery_create_user"
    # Never attempted user-creation type_text (only diskutil probe + fail).
    typed = [c for c in vm.calls if c[0] == "type_text"]
    assert len(typed) == 1  # only the data-volume probe


def test_missing_dvol_rw_sentinel_aborts():
    vm = FakeVMDriver(
        screens=["recovery", "terminal"],
        ocr_responses=[
            "WIZARD_SENTINEL DVOL=/Volumes/Foo\n",  # no DVOL_RW
        ],
    )
    rep = CapturingReporter()
    out = _run(vm, rep)
    assert out.status == "failed"
    assert out.phase == "recovery_create_user"


def test_sysadminctl_missing_shadow_falls_back_to_dscl():
    vm = FakeVMDriver(
        screens=["recovery", "terminal", "desktop"],
        ocr_responses=[
            # data volume OK
            "WIZARD_SENTINEL DVOL=/Volumes/X\nWIZARD_SENTINEL DVOL_RW=1\n",
            # sysadminctl: exit 0, plist exists, but NO SHADOW_OK
            "WIZARD_SENTINEL USER_CREATED=0\nWIZARD_SENTINEL USER_PLIST_OK\n",
            # dscl fallback: both sentinels present
            "WIZARD_SENTINEL DSCL_OK\nWIZARD_SENTINEL SHADOW_OK\n",
            # setup sentinels
            _sa_keys_sentinels(),
        ],
    )
    rep = CapturingReporter()
    out = _run(vm, rep)
    assert out.status == "ok"
    # Warning emitted about falling back
    assert any("sysadminctl" in m for m in rep.messages("warning"))
    assert any("dscl" in m.lower() for m in rep.messages())


def test_both_sysadminctl_and_dscl_fail_hard_error():
    vm = FakeVMDriver(
        screens=["recovery", "terminal"],
        ocr_responses=[
            "WIZARD_SENTINEL DVOL=/Volumes/X\nWIZARD_SENTINEL DVOL_RW=1\n",
            "WIZARD_SENTINEL USER_CREATED=1\n",   # sysadminctl fails
            "nothing useful\n",                    # dscl also fails
        ],
    )
    rep = CapturingReporter()
    out = _run(vm, rep)
    assert out.status == "failed"
    assert out.phase == "recovery_create_user"


def test_post_reboot_non_desktop_fails_verify_phase():
    vm = FakeVMDriver(
        screens=["recovery", "terminal", "setup_wizard"],  # <- bypass did not take
        ocr_responses=_happy_ocr_script(),
    )
    rep = CapturingReporter()
    out = _run(vm, rep)
    assert out.status == "failed"
    assert out.phase == "verify_desktop"


def test_gui_fallback_not_implemented():
    vm = FakeVMDriver()
    rep = CapturingReporter()
    with pytest.raises(NotImplementedError):
        _run(vm, rep, path="gui")


def test_unknown_path_returns_failed_outcome():
    vm = FakeVMDriver()
    rep = CapturingReporter()
    out = _run(vm, rep, path="nonsense")  # type: ignore[arg-type]
    assert out.status == "failed"
    assert "unknown" in out.message


# ---------------------------------------------------------------------
# Defensive: an exception in the driver must not propagate raw.
# ---------------------------------------------------------------------

class _BoomDriver(FakeVMDriver):
    def restart_without_mac_hdd(self):  # type: ignore[override]
        raise RuntimeError("kvm module gone")


def test_driver_exception_captured_as_failed_outcome():
    rep = CapturingReporter()
    out = _run(_BoomDriver(), rep)
    assert out.status == "failed"
    assert out.phase == "error"
    assert "kvm module gone" in out.message
