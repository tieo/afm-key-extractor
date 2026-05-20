"""Tests for the QEMU snapshot helpers (no real VM required).

The HMP socket is mocked at the `airtag_tracker.qmp.hmp` boundary so
tests verify the wrapper's behaviour, not QEMU's.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from airtag_tracker.vm import _snapshot


def _mock_running(running: bool):
    return patch("airtag_tracker.vm._snapshot.is_running", return_value=running)


def test_save_requires_running_vm():
    with _mock_running(False):
        with pytest.raises(_snapshot.VmError, match="not running"):
            _snapshot.save("foo")


def test_save_rejects_bad_label():
    with _mock_running(True):
        for bad in ("foo bar", "../etc", "foo;rm -rf", "", "a" * 65):
            with pytest.raises(_snapshot.VmError, match="Invalid snapshot label"):
                _snapshot.save(bad)


def test_save_returns_metadata_on_success():
    with _mock_running(True), \
         patch("airtag_tracker.qmp.hmp", return_value="") as h:
        result = _snapshot.save("pre_sa")
    assert result["label"] == "pre_sa"
    assert "elapsed_s" in result
    h.assert_called_once()
    assert "savevm pre_sa" in h.call_args[0][0]


def test_save_raises_when_hmp_errors():
    with _mock_running(True), \
         patch("airtag_tracker.qmp.hmp", return_value="Error: out of space"):
        with pytest.raises(_snapshot.VmError, match="out of space"):
            _snapshot.save("foo")


def test_load_invokes_loadvm():
    with _mock_running(True), \
         patch("airtag_tracker.qmp.hmp", return_value="") as h:
        _snapshot.load("foo")
    assert "loadvm foo" in h.call_args[0][0]


def test_delete_is_idempotent_when_absent():
    with _mock_running(True), \
         patch("airtag_tracker.qmp.hmp", return_value="qemu-system-x86_64: no such snapshot: foo"):
        # Absent snapshot shouldn't raise.
        result = _snapshot.delete("foo")
        assert result["deleted"] is True


def test_delete_raises_on_other_errors():
    with _mock_running(True), \
         patch("airtag_tracker.qmp.hmp", return_value="Error: disk write failed"):
        with pytest.raises(_snapshot.VmError, match="disk write failed"):
            _snapshot.delete("foo")


def test_list_all_parses_info_snapshots():
    sample = """List of snapshots present on all disks:
ID        TAG               VM SIZE                DATE       VM CLOCK     ICOUNT
1         pre_sa_create     567 MiB 2026-05-20 14:23:01   01:23:45.6
2         post_format       512 MiB 2026-05-20 13:00:00   00:30:00.0"""
    with _mock_running(True), \
         patch("airtag_tracker.qmp.hmp", return_value=sample):
        rows = _snapshot.list_all()
    assert len(rows) == 2
    assert rows[0]["tag"] == "pre_sa_create"
    assert rows[1]["tag"] == "post_format"


def test_list_all_empty_when_no_snapshots():
    with _mock_running(True), \
         patch("airtag_tracker.qmp.hmp", return_value=""):
        assert _snapshot.list_all() == []
