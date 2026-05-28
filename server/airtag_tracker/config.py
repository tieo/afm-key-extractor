"""Environment-driven configuration constants."""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("AIRTAG_DATA_DIR", "/var/lib/airtag-tracker"))
KEYS_DIR = DATA_DIR / "keys"
PLISTS_DIR = DATA_DIR / "plists"
DB_PATH = DATA_DIR / "locations.db"
ACCOUNT_PATH = DATA_DIR / "account.json"
ACCOUNT_ENC_PATH = DATA_DIR / "account.enc"
ANISETTE_PATH = DATA_DIR / "ani_libs.bin"
SETTINGS_PATH = DATA_DIR / "settings.json"
VM_PASSWORD_PATH = DATA_DIR / "vm-password"
VM_SSH_ENABLED_MARKER = DATA_DIR / "vm-ssh-enabled"
VM_ICLOUD_SIGNED_IN_MARKER = DATA_DIR / "vm-icloud-signed-in"

POLL_INTERVAL = int(os.environ.get("AIRTAG_POLL_INTERVAL", "900"))
# When true, the server automatically triggers a runtime run every POLL_INTERVAL
# seconds (as long as no flow is already running and credentials are configured).
AUTO_RUN = os.environ.get("AIRTAG_AUTO_RUN", "false").lower() in ("1", "true", "yes")
PORT = int(os.environ.get("AIRTAG_PORT", "8042"))
STATIC_DIR = Path(__file__).parent.parent / "static"

VM_ENABLED = os.environ.get("AIRTAG_VM_ENABLED", "false").lower() in ("1", "true", "yes")
VM_DIR = Path(os.environ.get("AIRTAG_VM_DIR", "/var/lib/airtag-tracker/osx-kvm"))
VNC_WS_PORT = int(os.environ.get("AIRTAG_VNC_WS_PORT", "6901"))
# Public URL for the noVNC web interface.  When empty, the UI falls back to
# http://localhost:VNC_WS_PORT which works for local dev.  Set to e.g.
# https://airtag-vnc.example.com when running behind a reverse proxy.
VNC_URL = os.environ.get("AIRTAG_VNC_URL", "")
VM_USERNAME = os.environ.get("AIRTAG_VM_USERNAME", "airtag")
VM_SSH_HOST = os.environ.get("AIRTAG_VM_SSH_HOST", "localhost")
VM_SSH_PORT = int(os.environ.get("AIRTAG_VM_SSH_PORT", "2222"))

# macOS version running in the VM.  Selects the adapter (14=Sonoma, 15=Sequoia).
MACOS_VERSION = int(os.environ.get("AIRTAG_MACOS_VERSION", "14"))

# Pre-configured Apple ID credentials.  When set, the start-runtime and
# resume-runtime endpoints use these as defaults; the UI form fields become
# optional.  Set via AIRTAG_APPLE_EMAIL / AIRTAG_APPLE_PASSWORD env vars
# (or a .env file read by Docker Compose via env_file: .env).
APPLE_EMAIL = os.environ.get("AIRTAG_APPLE_EMAIL", "")
APPLE_PASSWORD = os.environ.get("AIRTAG_APPLE_PASSWORD", "")
# Last 4+ digits of the phone number Apple should SMS the 2FA code to.
# When Apple shows a list of trusted numbers, the automation clicks the one
# containing this suffix.  Leave empty to accept whichever Apple pre-selects.
APPLE_SMS_PHONE_SUFFIX = os.environ.get("AIRTAG_SMS_PHONE_SUFFIX", "")
# iPhone passcode to approve iCloud data sync ("Some iCloud Data Isn't Syncing").
IPHONE_PASSCODE = os.environ.get("AIRTAG_IPHONE_PASSCODE", "")

QEMU_BINARY = os.environ.get("AIRTAG_QEMU_BINARY", "qemu-system-x86_64")

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
