"""Unit tests for the macOS version adapter model.

All tests run without a VM, a real keychain, or any hardware.
Hardware calls (qmp, vm_ui, ssh) are patched at the module level.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from airtag_tracker.macos_adapter import (
    MacOSAdapter,
    SonomaAdapter,
    SequoiaAdapter,
    SUPPORTED_VERSIONS,
    get_adapter,
    get_active_adapter,
)
from airtag_tracker.automation.context import AutomationContext
from airtag_tracker.automation.states import FlowKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(adapter=None) -> AutomationContext:
    return AutomationContext(
        flow_kind=FlowKind.INSTALL,
        vm_password="testpw",
        macos_adapter=adapter or SonomaAdapter(),
    )


# ---------------------------------------------------------------------------
# Factory / registry
# ---------------------------------------------------------------------------

def test_supported_versions_includes_sonoma_and_sequoia():
    assert 14 in SUPPORTED_VERSIONS
    assert 15 in SUPPORTED_VERSIONS


def test_get_adapter_14_returns_sonoma():
    a = get_adapter(14)
    assert isinstance(a, SonomaAdapter)


def test_get_adapter_15_returns_sequoia():
    a = get_adapter(15)
    assert isinstance(a, SequoiaAdapter)


def test_get_adapter_unsupported_raises():
    with pytest.raises(ValueError, match="Unsupported macOS version 99"):
        get_adapter(99)


def test_get_active_adapter_default_is_sonoma(monkeypatch):
    monkeypatch.delenv("AIRTAG_MACOS_VERSION", raising=False)
    # Reload config to pick up the unset env var.
    import importlib
    import airtag_tracker.config as cfg
    monkeypatch.setattr(cfg, "MACOS_VERSION", 14)
    a = get_active_adapter()
    assert isinstance(a, SonomaAdapter)


def test_get_active_adapter_respects_env_var(monkeypatch):
    """Sequoia is registered but not yet implemented — get_active_adapter refuses it."""
    import airtag_tracker.config as cfg
    monkeypatch.setattr(cfg, "MACOS_VERSION", 15)
    with pytest.raises(RuntimeError, match="not.*yet.*fully supported"):
        get_active_adapter()


def test_get_active_adapter_rejects_unimplemented(monkeypatch):
    """get_active_adapter() raises a clear error for incomplete adapters."""
    import airtag_tracker.config as cfg
    monkeypatch.setattr(cfg, "MACOS_VERSION", 15)
    with pytest.raises(RuntimeError, match="Sequoia.*not.*yet"):
        get_active_adapter()


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def test_sonoma_version():
    assert SonomaAdapter().version == 14


def test_sequoia_version():
    assert SequoiaAdapter().version == 15


def test_sonoma_name():
    assert SonomaAdapter().name == "Sonoma"


def test_sequoia_name():
    assert SequoiaAdapter().name == "Sequoia"


def test_display_name_format():
    assert SonomaAdapter().display_name == "Sonoma (macOS 14)"
    assert SequoiaAdapter().display_name == "Sequoia (macOS 15)"


# ---------------------------------------------------------------------------
# Golden image paths
# ---------------------------------------------------------------------------

def test_sonoma_golden_image_path():
    vm_dir = Path("/data/osx-kvm")
    path = SonomaAdapter().golden_image_path(vm_dir)
    assert path == vm_dir / "mac_hdd_golden_sonoma.img"


def test_sequoia_golden_image_path():
    vm_dir = Path("/data/osx-kvm")
    path = SequoiaAdapter().golden_image_path(vm_dir)
    assert path == vm_dir / "mac_hdd_golden_sequoia.img"


def test_sonoma_base_system_path():
    vm_dir = Path("/data/osx-kvm")
    assert SonomaAdapter().base_system_path(vm_dir) == vm_dir / "BaseSystem_sonoma.img"


def test_sequoia_base_system_path():
    vm_dir = Path("/data/osx-kvm")
    assert SequoiaAdapter().base_system_path(vm_dir) == vm_dir / "BaseSystem_sequoia.img"


def test_golden_image_paths_are_distinct():
    vm_dir = Path("/data/osx-kvm")
    assert SonomaAdapter().golden_image_path(vm_dir) != SequoiaAdapter().golden_image_path(vm_dir)


# ---------------------------------------------------------------------------
# pre_reboot_recovery_setup
# ---------------------------------------------------------------------------

def test_sonoma_pre_reboot_is_noop():
    adapter = SonomaAdapter()
    ctx = _ctx(adapter)
    # SonomaAdapter.pre_reboot_recovery_setup is `pass` — just verify it completes.
    adapter.pre_reboot_recovery_setup(ctx)


def test_sequoia_pre_reboot_types_csrutil():
    adapter = SequoiaAdapter()
    ctx = _ctx(adapter)
    # qmp and events are lazy-imported inside the method; patch at their real paths.
    with patch("airtag_tracker.qmp.type_text") as mock_type, \
         patch("airtag_tracker.qmp.send_keys") as mock_send, \
         patch("airtag_tracker.macos_adapter.time") as mock_time, \
         patch("airtag_tracker.events.emit"):
        adapter.pre_reboot_recovery_setup(ctx)
        mock_type.assert_called_once_with("csrutil enable --without nvram", gap_s=0.05)
        mock_send.assert_called_once_with(["ret"])


# ---------------------------------------------------------------------------
# extract_beacon_key
# ---------------------------------------------------------------------------

def test_sequoia_extract_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="not yet implemented"):
        SequoiaAdapter().extract_beacon_key(vm_password="pw")


@patch("airtag_tracker.vm_ssh.run")
@patch("airtag_tracker.macos_adapter.time")
def test_sonoma_extract_returns_key_when_no_dialog(mock_time, mock_ssh_run):
    """Happy path: key file populated immediately, no ACL dialog."""
    # qmp and vm_ui are lazy-imported inside extract_beacon_key; patch at real paths.
    mock_qmp_ctx = MagicMock()
    mock_qmp_ctx.__enter__ = MagicMock(return_value=MagicMock())
    mock_qmp_ctx.__exit__ = MagicMock(return_value=False)

    ready = MagicMock(stdout="READY\n")
    key_out = MagicMock(stdout="deadbeef1234\n")
    mock_ssh_run.side_effect = [ready, key_out]

    with patch("airtag_tracker.qmp.qmp", return_value=mock_qmp_ctx), \
         patch("airtag_tracker.qmp.type_text"), \
         patch("airtag_tracker.qmp.send_keys"), \
         patch("airtag_tracker.qmp.screendump"), \
         patch("airtag_tracker.vm_ui.screen_text", return_value="some output"), \
         patch("pathlib.Path.unlink"):
        key = SonomaAdapter().extract_beacon_key(vm_password="testpw")

    assert key == "deadbeef1234"


@patch("airtag_tracker.vm_ssh.run")
@patch("airtag_tracker.macos_adapter.time")
def test_sonoma_extract_raises_on_empty_key(mock_time, mock_ssh_run):
    """Key file empty after all polling attempts → RuntimeError."""
    mock_qmp_ctx = MagicMock()
    mock_qmp_ctx.__enter__ = MagicMock(return_value=MagicMock())
    mock_qmp_ctx.__exit__ = MagicMock(return_value=False)

    not_ready = MagicMock(stdout="")
    empty_key = MagicMock(stdout="")
    err_msg = MagicMock(stdout="errSecAuthFailed")
    mock_ssh_run.side_effect = [not_ready] * 24 + [empty_key, err_msg]

    with patch("airtag_tracker.qmp.qmp", return_value=mock_qmp_ctx), \
         patch("airtag_tracker.qmp.type_text"), \
         patch("airtag_tracker.qmp.send_keys"), \
         patch("airtag_tracker.qmp.screendump"), \
         patch("airtag_tracker.vm_ui.screen_text", return_value=""), \
         patch("pathlib.Path.unlink"):
        with pytest.raises(RuntimeError, match="beacon key empty"):
            SonomaAdapter().extract_beacon_key(vm_password="testpw")


# ---------------------------------------------------------------------------
# AutomationContext adapter injection
# ---------------------------------------------------------------------------

def test_context_gets_default_adapter():
    import airtag_tracker.config as cfg
    original = cfg.MACOS_VERSION
    cfg.MACOS_VERSION = 14
    try:
        ctx = AutomationContext(
            flow_kind=FlowKind.INSTALL,
            vm_password="pw",
        )
        assert isinstance(ctx.adapter, SonomaAdapter)
    finally:
        cfg.MACOS_VERSION = original


def test_context_accepts_injected_adapter():
    adapter = SequoiaAdapter()
    ctx = AutomationContext(
        flow_kind=FlowKind.RUNTIME,
        vm_password="pw",
        macos_adapter=adapter,
    )
    assert ctx.adapter is adapter


def test_adapters_are_independent_instances():
    a1 = get_adapter(14)
    a2 = get_adapter(14)
    assert a1 is not a2
