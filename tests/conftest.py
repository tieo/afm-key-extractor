"""Pytest configuration — set writable data dir before any module imports."""

from __future__ import annotations

import os
import tempfile

# Must be set before airtag_tracker.config is imported so DATA_DIR resolves
# to a writable temp path in CI/dev environments that lack /var/lib/airtag-tracker.
_tmp = tempfile.mkdtemp(prefix="airtag_test_")
os.environ.setdefault("AIRTAG_DATA_DIR", _tmp)
os.environ.setdefault("AIRTAG_VM_DIR", _tmp)
