"""Environment-driven configuration constants."""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("AIRTAG_DATA_DIR", "/var/lib/airtag-tracker"))
KEYS_DIR = DATA_DIR / "keys"
DB_PATH = DATA_DIR / "locations.db"
ACCOUNT_PATH = DATA_DIR / "account.json"
ACCOUNT_ENC_PATH = DATA_DIR / "account.enc"
ANISETTE_PATH = DATA_DIR / "ani_libs.bin"
SETTINGS_PATH = DATA_DIR / "settings.json"
VM_PASSWORD_PATH = DATA_DIR / "vm-password"

POLL_INTERVAL = int(os.environ.get("AIRTAG_POLL_INTERVAL", "900"))
PORT = int(os.environ.get("AIRTAG_PORT", "8042"))
STATIC_DIR = Path(__file__).parent.parent / "static"

VM_ENABLED = os.environ.get("AIRTAG_VM_ENABLED", "false") == "true"
VM_DIR = Path(os.environ.get("AIRTAG_VM_DIR", "/var/lib/airtag-tracker/osx-kvm"))
VNC_WS_PORT = int(os.environ.get("AIRTAG_VNC_WS_PORT", "6901"))
VM_USERNAME = os.environ.get("AIRTAG_VM_USERNAME", "airtag")

QMP_SOCK = "/tmp/airtag-vm-qmp.sock"
MONITOR_SOCK = "/tmp/airtag-vm-monitor.sock"
VM_PID_FILE = Path("/tmp/airtag-vm-setup.pid")

DEFAULT_SETTINGS = {
    "idle_interval": POLL_INTERVAL,
    "active_interval": 120,
    "movement_threshold": 50,
    "cooldown_polls": 5,
    "adaptive": True,
}
