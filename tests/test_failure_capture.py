"""Tests for the automatic failure-capture path.

Verifies that ``failure_capture.capture()`` writes the expected artifacts
under DATA_DIR/failures/<state>_<ts>/, that it's robust to sub-step
failures, and that the rotate keeps at most MAX_KEPT directories.

VM-side calls (screendump, snapshot.save) are mocked.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR + FAILURE_DIR at a temp dir for one test."""
    from airtag_tracker import config
    from airtag_tracker.automation import failure_capture

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(failure_capture, "FAILURE_DIR", tmp_path / "failures")
    yield tmp_path


def _patch_screendump(out_text: bytes = b"P6\n2 2\n255\nXXXXXXXXXXXX"):
    """Make vm_ui._screendump return a fake PPM file path."""
    import tempfile

    def fake_screendump():
        f = tempfile.NamedTemporaryFile(suffix=".ppm", delete=False)
        f.write(out_text)
        f.close()
        return f.name

    return patch("airtag_tracker.vm_ui._screendump", side_effect=fake_screendump)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_capture_writes_screen_log_meta(isolated_data_dir):
    from airtag_tracker.automation import failure_capture

    fake_events = [
        {"ts": "2026-05-21T01:00:00Z", "level": "info", "cat": "engine", "msg": "ok"},
        {"ts": "2026-05-21T01:00:01Z", "level": "error", "cat": "engine", "msg": "boom"},
    ]

    with _patch_screendump(), \
         patch("airtag_tracker.events.snapshot", return_value=fake_events), \
         patch("airtag_tracker.vm.is_running", return_value=False):  # skip snapshot
        result = failure_capture.capture("sa_create_account", "boom")

    out = Path(result["dir"])
    assert out.exists() and out.is_dir()
    assert (out / "screen.png").exists()
    assert (out / "log.txt").exists()
    assert (out / "meta.json").exists()
    meta = json.loads((out / "meta.json").read_text())
    assert meta["state"] == "sa_create_account"
    assert meta["error"] == "boom"
    # No snapshot when VM isn't running.
    assert "snapshot" not in result
    log = (out / "log.txt").read_text()
    assert "boom" in log
    assert "engine" in log


def test_capture_takes_snapshot_when_vm_running(isolated_data_dir):
    from airtag_tracker.automation import failure_capture

    with _patch_screendump(), \
         patch("airtag_tracker.events.snapshot", return_value=[]), \
         patch("airtag_tracker.vm.is_running", return_value=True), \
         patch("airtag_tracker.vm.snapshot.save",
               return_value={"label": "fail_sa_create_account_X"}) as save:
        result = failure_capture.capture("sa_create_account", "boom")

    save.assert_called_once()
    label_arg = save.call_args[0][0]
    assert label_arg.startswith("fail_sa_create_account_")
    assert len(label_arg) <= 64
    assert result["snapshot"] == label_arg


def test_capture_safe_when_screendump_raises(isolated_data_dir):
    from airtag_tracker.automation import failure_capture

    with patch("airtag_tracker.vm_ui._screendump",
               side_effect=RuntimeError("vm dead")), \
         patch("airtag_tracker.events.snapshot", return_value=[]), \
         patch("airtag_tracker.vm.is_running", return_value=False):
        # Must not raise — failure-capture is best-effort.
        result = failure_capture.capture("foo", "bar")

    out = Path(result["dir"])
    assert (out / "meta.json").exists()
    # No screen artifact when screendump fails.
    assert not (out / "screen.png").exists()


def test_capture_safe_when_snapshot_raises(isolated_data_dir):
    from airtag_tracker.automation import failure_capture

    with _patch_screendump(), \
         patch("airtag_tracker.events.snapshot", return_value=[]), \
         patch("airtag_tracker.vm.is_running", return_value=True), \
         patch("airtag_tracker.vm.snapshot.save", side_effect=RuntimeError("savevm gone")):
        result = failure_capture.capture("foo", "bar")

    # No snapshot key recorded; screen + meta still saved.
    assert "snapshot" not in result


def test_capture_sanitises_state_value(isolated_data_dir):
    from airtag_tracker.automation import failure_capture

    with _patch_screendump(), \
         patch("airtag_tracker.events.snapshot", return_value=[]), \
         patch("airtag_tracker.vm.is_running", return_value=False):
        result = failure_capture.capture("path/with..slashes", "x")

    # Dir name has unsafe chars replaced with underscores.
    assert "/" not in Path(result["dir"]).name
    assert ".." not in Path(result["dir"]).name


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def test_capture_rotates_to_max_kept(isolated_data_dir):
    from airtag_tracker.automation import failure_capture

    # Capture MAX_KEPT + 3 — only MAX_KEPT should remain.
    with _patch_screendump(), \
         patch("airtag_tracker.events.snapshot", return_value=[]), \
         patch("airtag_tracker.vm.is_running", return_value=False):
        for i in range(failure_capture.MAX_KEPT + 3):
            failure_capture.capture(f"state_{i}", "err")
            time.sleep(0.02)  # ensure distinct mtimes

    dirs = [p for p in failure_capture.FAILURE_DIR.iterdir() if p.is_dir()]
    assert len(dirs) == failure_capture.MAX_KEPT
    # The kept dirs should be the latest ones (state_3..state_7 for MAX_KEPT=5).
    names = sorted(d.name for d in dirs)
    assert all("state_" in n for n in names)
    # Earliest 3 are gone.
    assert not any(n.startswith("state_0_") or n.startswith("state_1_")
                   or n.startswith("state_2_") for n in names)
