"""AirTag tracker server — polls Apple's Find My network and serves location history."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import os
import socket
import sqlite3
import subprocess as sp
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytesseract
from findmy import AppleAccount, FindMyAccessory, LocalAnisetteProvider
from findmy.reports import LoginState, SyncSmsSecondFactor
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

DATA_DIR = Path(os.environ.get("AIRTAG_DATA_DIR", "/var/lib/airtag-tracker"))
KEYS_DIR = DATA_DIR / "keys"
DB_PATH = DATA_DIR / "locations.db"
ACCOUNT_PATH = DATA_DIR / "account.json"
ANISETTE_PATH = DATA_DIR / "ani_libs.bin"
POLL_INTERVAL = int(os.environ.get("AIRTAG_POLL_INTERVAL", "900"))  # 15 min default
PORT = int(os.environ.get("AIRTAG_PORT", "8042"))
STATIC_DIR = Path(__file__).parent / "static"
VM_ENABLED = os.environ.get("AIRTAG_VM_ENABLED", "false") == "true"
VM_DIR = Path(os.environ.get("AIRTAG_VM_DIR", "/var/lib/airtag-tracker/osx-kvm"))
VNC_WS_PORT = int(os.environ.get("AIRTAG_VNC_WS_PORT", "6901"))

app = Flask(__name__, static_folder=str(STATIC_DIR))

# In-memory state for login flow
_pending_account = None
_pending_2fa_methods = None

# --- Event log ---
_event_log = []
_event_log_lock = threading.Lock()
MAX_EVENTS = 500


def emit(level, category, message):
    """Add an event to the in-app log and also log to stdout."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "level": level,
        "cat": category,
        "msg": message,
    }
    with _event_log_lock:
        _event_log.append(entry)
        if len(_event_log) > MAX_EVENTS:
            _event_log[:] = _event_log[-MAX_EVENTS:]
    getattr(log, level, log.info)(f"[{category}] {message}")


SETTINGS_PATH = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "idle_interval": POLL_INTERVAL,  # seconds between polls when stationary
    "active_interval": 120,  # seconds between polls when moving
    "movement_threshold": 50,  # meters — distance to consider "moved"
    "cooldown_polls": 5,  # polls with no movement before returning to idle
}

_settings_lock = threading.Lock()
_poll_state = {
    "moving": False,
    "idle_count": 0,  # consecutive polls with no movement
    "last_positions": {},  # airtag_id -> (lat, lon)
    "current_interval": POLL_INTERVAL,
    "last_poll": None,
}


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH) as f:
                saved = json.load(f)
            return {**DEFAULT_SETTINGS, **saved}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def haversine(lat1, lon1, lat2, lon2):
    """Distance in meters between two GPS coordinates."""
    R = 6371000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            airtag_id TEXT NOT NULL,
            airtag_name TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            accuracy INTEGER,
            timestamp TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_locations_airtag_time
        ON locations (airtag_id, timestamp)
    """)
    db.commit()
    db.close()


def load_airtags():
    """Load all AirTag key files from the keys directory."""
    tags = []
    if not KEYS_DIR.exists():
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        return tags
    for f in KEYS_DIR.glob("*.json"):
        try:
            tag = FindMyAccessory.from_json(str(f))
            tags.append((f.stem, tag))
            emit("info", "keys", f"Loaded AirTag: {f.stem}")
        except Exception as e:
            emit("error", "keys", f"Failed to load {f.name}: {e}")
    return tags


def get_account():
    """Get or create an authenticated Apple account."""
    ani = LocalAnisetteProvider(libs_path=str(ANISETTE_PATH))
    if ACCOUNT_PATH.exists():
        try:
            acc = AppleAccount.from_json(str(ACCOUNT_PATH), anisette=ani)
            emit("info", "account", "Restored Apple account session")
            return acc
        except Exception as e:
            emit("warning", "account", f"Failed to restore session: {e}")
    return None


def save_locations(airtag_id, airtag_name, reports):
    """Save location reports to the database."""
    db = sqlite3.connect(str(DB_PATH))
    now = datetime.now(UTC).isoformat()
    for report in reports:
        db.execute(
            "INSERT INTO locations (airtag_id, airtag_name, latitude, longitude, accuracy, timestamp, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                airtag_id,
                airtag_name,
                report.latitude,
                report.longitude,
                report.horizontal_accuracy,
                report.timestamp.isoformat(),
                now,
            ),
        )
    db.commit()
    db.close()


def poll_locations():
    """Fetch latest locations for all AirTags. Returns True if any moved."""
    emit("info", "poll", "Starting location poll")
    acc = get_account()
    if not acc:
        emit("warning", "poll", "No Apple account configured, skipping poll")
        return False

    tags = load_airtags()
    if not tags:
        emit("info", "poll", "No AirTags configured, skipping poll")
        return False

    settings = load_settings()
    any_moved = False
    total_reports = 0

    try:
        accessories = [tag for _, tag in tags]
        emit("info", "poll", f"Querying Apple Find My for {len(accessories)} tag(s)")
        history = acc.fetch_location_history(accessories)

        for (tag_id, tag), reports in zip(
            tags, [history.get(t, []) for t in accessories], strict=False
        ):
            name = getattr(tag, "name", tag_id)
            if reports:
                save_locations(tag_id, name, reports)
                total_reports += len(reports)

                latest = max(reports, key=lambda r: r.timestamp)
                last_pos = _poll_state["last_positions"].get(tag_id)
                if last_pos:
                    dist = haversine(last_pos[0], last_pos[1], latest.latitude, latest.longitude)
                    if dist > settings["movement_threshold"]:
                        any_moved = True
                        emit(
                            "info",
                            "movement",
                            f"{name} moved {dist:.0f}m (threshold: {settings['movement_threshold']}m)",
                        )
                    else:
                        emit(
                            "info",
                            "poll",
                            f"{name}: {len(reports)} report(s), stationary ({dist:.0f}m)",
                        )
                else:
                    emit(
                        "info",
                        "poll",
                        f"{name}: {len(reports)} report(s), first position recorded",
                    )
                _poll_state["last_positions"][tag_id] = (
                    latest.latitude,
                    latest.longitude,
                )
            else:
                emit("info", "poll", f"{name}: no new reports")

        acc.to_json(str(ACCOUNT_PATH))
        for tag_id, tag in tags:
            tag.to_json(str(KEYS_DIR / f"{tag_id}.json"))

        emit(
            "info",
            "poll",
            f"Poll complete: {total_reports} report(s) from {len(tags)} tag(s)",
        )

    except Exception as e:
        emit("error", "poll", f"Poll failed: {e}")

    return any_moved


def poll_loop():
    """Background thread with adaptive polling interval."""
    emit("info", "system", "Poll loop started")
    while True:
        settings = load_settings()
        try:
            moved = poll_locations()
            _poll_state["last_poll"] = datetime.now(UTC).isoformat()

            with _settings_lock:
                prev_moving = _poll_state["moving"]
                if settings.get("adaptive", True) and moved:
                    _poll_state["moving"] = True
                    _poll_state["idle_count"] = 0
                    _poll_state["current_interval"] = settings["active_interval"]
                    if not prev_moving:
                        emit(
                            "info",
                            "adaptive",
                            f"Switching to active polling (every {settings['active_interval']}s)",
                        )
                elif settings.get("adaptive", True):
                    _poll_state["idle_count"] += 1
                    if _poll_state["idle_count"] >= settings["cooldown_polls"] and prev_moving:
                        _poll_state["moving"] = False
                        _poll_state["current_interval"] = settings["idle_interval"]
                        emit(
                            "info",
                            "adaptive",
                            f"No movement for {settings['cooldown_polls']} polls, returning to idle (every {settings['idle_interval']}s)",
                        )
                    elif _poll_state["idle_count"] >= settings["cooldown_polls"]:
                        _poll_state["moving"] = False
                        _poll_state["current_interval"] = settings["idle_interval"]
                else:
                    _poll_state["moving"] = False
                    _poll_state["current_interval"] = settings["idle_interval"]

        except Exception as e:
            emit("error", "poll", f"Poll loop error: {e}")

        interval = _poll_state["current_interval"]
        time.sleep(interval)


# --- API routes ---


@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/api/log")
def get_log():
    """Get recent event log entries."""
    since = request.args.get("since")
    cat = request.args.get("cat")
    limit = int(request.args.get("limit", "100"))
    with _event_log_lock:
        entries = list(_event_log)
    if since:
        entries = [e for e in entries if e["ts"] > since]
    if cat:
        entries = [e for e in entries if e["cat"] == cat]
    return jsonify(entries[-limit:])


@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Get current polling settings and state."""
    settings = load_settings()
    return jsonify(
        {
            **settings,
            "adaptive": settings.get("adaptive", True),
            "state": {
                "moving": _poll_state["moving"],
                "current_interval": _poll_state["current_interval"],
                "idle_count": _poll_state["idle_count"],
                "last_poll": _poll_state["last_poll"],
            },
        }
    )


@app.route("/api/settings", methods=["PUT"])
def update_settings():
    """Update polling settings."""
    data = request.get_json()
    settings = load_settings()
    allowed = {
        "idle_interval",
        "active_interval",
        "movement_threshold",
        "cooldown_polls",
        "adaptive",
    }
    for key in allowed:
        if key in data:
            settings[key] = data[key]
    save_settings(settings)

    # Apply new idle interval immediately if not moving
    with _settings_lock:
        if not _poll_state["moving"]:
            _poll_state["current_interval"] = settings["idle_interval"]

    return jsonify(settings)


@app.route("/api/airtags")
def list_airtags():
    """List all known AirTags with their latest position."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT airtag_id, airtag_name, latitude, longitude, accuracy, timestamp
        FROM locations
        WHERE id IN (
            SELECT MAX(id) FROM locations GROUP BY airtag_id
        )
    """).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/airtags/<airtag_id>/history")
def airtag_history(airtag_id):
    """Get location history for a specific AirTag."""
    since = request.args.get("since", "1970-01-01T00:00:00")
    limit = int(request.args.get("limit", "1000"))
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT latitude, longitude, accuracy, timestamp FROM locations "
        "WHERE airtag_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
        (airtag_id, since, limit),
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/poll", methods=["POST"])
def trigger_poll():
    """Manually trigger a location poll."""
    emit("info", "poll", "Manual poll triggered")
    threading.Thread(target=poll_locations, daemon=True).start()
    return jsonify({"status": "polling"})


@app.route("/api/extract-keys", methods=["POST"])
def trigger_extract():
    """Trigger macOS VM to extract AirTag keys."""
    emit("info", "vm", "Key extraction triggered")
    try:
        result = sp.run(
            ["systemctl", "start", "--no-block", "airtag-extract-keys"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            emit("error", "vm", f"Failed to start extraction: {result.stderr.strip()}")
            return jsonify({"status": "error", "message": result.stderr.strip()}), 500
        emit("info", "vm", "Key extraction service started, VM booting")
        threading.Thread(
            target=_tail_journal, args=("airtag-extract-keys", "vm"), daemon=True
        ).start()
        return jsonify(
            {
                "status": "started",
                "message": "Key extraction started. This takes a few minutes.",
            }
        )
    except Exception as e:
        emit("error", "vm", f"Extract trigger error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# --- VM setup management (noVNC) ---


def _systemctl(action, service):
    return sp.run(
        [
            "/run/wrappers/bin/sudo",
            "/run/current-system/sw/bin/systemctl",
            action,
            service,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _tail_journal(unit, category):
    """Background thread: tail journald for a systemd unit and emit events."""
    try:
        proc = sp.Popen(
            ["journalctl", "-u", unit, "-f", "-n", "0", "--no-hostname", "-o", "cat"],
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            text=True,
        )
        emit("info", category, f"Streaming logs for {unit}")
        for line in proc.stdout:
            line = line.strip()
            if line:
                emit("info", category, line)
            # Stop if the service is no longer active
            check = sp.run(["systemctl", "is-active", unit], capture_output=True, text=True)
            if check.stdout.strip() not in ("active", "activating"):
                break
        proc.terminate()
        rc = sp.run(
            ["systemctl", "show", unit, "-p", "ExecMainStatus", "--value"],
            capture_output=True,
            text=True,
        )
        exit_code = rc.stdout.strip()
        if exit_code == "0":
            emit("info", category, f"{unit} completed successfully")
        else:
            emit("error", category, f"{unit} exited with code {exit_code}")
    except Exception as e:
        emit("error", category, f"Journal tail error: {e}")


@app.route("/api/vm/status")
def vm_status():
    """Check VM setup state."""
    if not VM_ENABLED:
        return jsonify({"enabled": False})

    vm_provisioned = (VM_DIR / "mac_hdd_ng.img").exists()
    vm_password = (DATA_DIR / "vm-password").exists()
    # Check if setup VM is running (QEMU with VNC)
    pid_file = Path("/tmp/airtag-vm-setup.pid")
    vm_running = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            vm_running = True
        except (ValueError, ProcessLookupError):
            pid_file.unlink(missing_ok=True)

    return jsonify(
        {
            "enabled": True,
            "provisioned": vm_provisioned,
            "setup_complete": vm_password,
            "vm_running": vm_running,
            "vnc_ws_port": VNC_WS_PORT,
        }
    )


@app.route("/api/vm/start-setup", methods=["POST"])
def vm_start_setup():
    """Start macOS VM with VNC for initial setup (install macOS, sign into Apple ID)."""
    if not VM_ENABLED:
        return jsonify({"error": "VM not enabled"}), 400

    if not (VM_DIR / "mac_hdd_ng.img").exists():
        return jsonify({"error": "VM not provisioned yet. Waiting for auto-provision."}), 400

    pid_file = Path("/tmp/airtag-vm-setup.pid")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return jsonify({"status": "already_running", "vnc_ws_port": VNC_WS_PORT})
        except (ValueError, ProcessLookupError):
            pid_file.unlink(missing_ok=True)

    has_base_system = (VM_DIR / "BaseSystem.img").exists()
    emit("info", "vm", f"Starting VM for setup (installer media: {has_base_system})")

    qemu_args = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-m",
        "8192",
        "-cpu",
        "Skylake-Client,-hle,-rtm,kvm=on,vendor=GenuineIntel,+invtsc,vmware-cpuid-freq=on,+ssse3,+sse4.2,+popcnt,+avx,+aes,+xsave,+xsaveopt,check",
        "-machine",
        "q35",
        "-device",
        "qemu-xhci,id=xhci",
        "-device",
        "usb-kbd,bus=xhci.0",
        "-device",
        "usb-tablet,bus=xhci.0",
        "-smp",
        "4,cores=2",
        "-global",
        "ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off",
        "-device",
        "isa-applesmc,osk=ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc",
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={VM_DIR}/OVMF_CODE_4M.fd",
        "-drive",
        f"if=pflash,format=raw,file={VM_DIR}/OVMF_VARS-1920x1080.fd",
        "-smbios",
        "type=2",
        "-device",
        "ich9-ahci,id=sata",
        "-drive",
        f"id=OpenCoreBoot,if=none,snapshot=on,format=qcow2,file={VM_DIR}/OpenCore/OpenCore.qcow2",
        "-device",
        "ide-hd,bus=sata.2,drive=OpenCoreBoot",
        "-drive",
        f"id=MacHDD,if=none,file={VM_DIR}/mac_hdd_ng.img,format=qcow2",
        "-device",
        "ide-hd,bus=sata.4,drive=MacHDD",
        "-netdev",
        "user,id=net0,hostfwd=tcp::2222-:22",
        "-device",
        "vmxnet3,netdev=net0,id=net0,mac=52:54:00:c9:18:27",
        "-device",
        "vmware-svga",
        "-vnc",
        "127.0.0.1:1",
        "-monitor",
        "unix:/tmp/airtag-vm-monitor.sock,server,nowait",
        "-qmp",
        "unix:/tmp/airtag-vm-qmp.sock,server,nowait",
        "-daemonize",
        "-pidfile",
        "/tmp/airtag-vm-setup.pid",
    ]

    # Add installer media if present (first install only)
    if has_base_system:
        qemu_args.extend(
            [
                "-drive",
                f"id=InstallMedia,if=none,file={VM_DIR}/BaseSystem.img,format=raw",
                "-device",
                "ide-hd,bus=sata.3,drive=InstallMedia",
            ]
        )

    try:
        result = sp.run(qemu_args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            emit("error", "vm", f"QEMU failed to start: {result.stderr}")
            return jsonify({"error": f"Failed to start VM: {result.stderr}"}), 500

        _systemctl("start", "airtag-novnc")
        emit("info", "vm", f"VM started, noVNC proxy active on port {VNC_WS_PORT}")

        # Auto-start installation if installer media is present (first install)
        if has_base_system:
            threading.Thread(target=_auto_install_worker, daemon=True).start()

        return jsonify(
            {
                "status": "started",
                "vnc_ws_port": VNC_WS_PORT,
                "auto_install": has_base_system,
            }
        )
    except Exception as e:
        emit("error", "vm", f"VM start error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/stop", methods=["POST"])
def vm_stop():
    """Stop the setup VM."""
    emit("info", "vm", "Stopping VM")
    pid_file = Path("/tmp/airtag-vm-setup.pid")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)  # SIGTERM
            emit("info", "vm", f"Sent SIGTERM to QEMU (PID {pid})")
        except (ValueError, ProcessLookupError):
            emit("info", "vm", "VM process already gone")
        pid_file.unlink(missing_ok=True)

    _monitor_disconnect()
    _qmp_disconnect()
    _systemctl("stop", "airtag-novnc")
    return jsonify({"status": "stopped"})


# --- QEMU monitor helpers for VM automation ---
MONITOR_SOCK = "/tmp/airtag-vm-monitor.sock"
_monitor_lock = threading.Lock()
_monitor_sock = None


def _monitor_connect():
    """Get or create a persistent connection to QEMU monitor."""
    global _monitor_sock
    if _monitor_sock is not None:
        return _monitor_sock
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(MONITOR_SOCK)
        s.recv(4096)  # read greeting
        _monitor_sock = s
        return s
    except Exception as e:
        emit("error", "vm", f"Monitor connect failed: {e}")
        _monitor_sock = None
        return None


def _monitor_disconnect():
    global _monitor_sock
    if _monitor_sock:
        with contextlib.suppress(Exception):
            _monitor_sock.close()
        _monitor_sock = None


def _monitor_cmd(cmd):
    """Send a command to QEMU's human monitor interface (persistent connection)."""
    with _monitor_lock:
        sock = _monitor_connect()
        if not sock:
            return None
        try:
            sock.sendall(f"{cmd}\n".encode())
            time.sleep(0.1)
            resp = sock.recv(4096).decode(errors="replace")
            return resp
        except Exception as e:
            emit("error", "vm", f"Monitor command failed: {e}")
            _monitor_disconnect()
            return None


def _send_key(key, delay=0.15):
    """Send a single keystroke via QEMU monitor."""
    emit("info", "vm", f"Key: {key}")
    _monitor_cmd(f"sendkey {key}")
    time.sleep(delay)


def _type_text(text):
    """Type a string character by character via QEMU monitor."""
    emit("info", "vm", f"Type: {text!r}")
    keymap = {
        " ": "spc",
        "\n": "ret",
        "-": "minus",
        "=": "equal",
        ".": "dot",
        ",": "comma",
        "/": "slash",
        "\\": "backslash",
        "[": "bracket_left",
        "]": "bracket_right",
        ";": "semicolon",
        "'": "apostrophe",
        "`": "grave_accent",
        "0": "0",
        "1": "1",
        "2": "2",
        "3": "3",
        "4": "4",
        "5": "5",
        "6": "6",
        "7": "7",
        "8": "8",
        "9": "9",
    }
    shift_keymap = {
        '"': "apostrophe",
        "!": "1",
        "@": "2",
        "#": "3",
        "$": "4",
        "%": "5",
        "^": "6",
        "&": "7",
        "*": "8",
        "(": "9",
        ")": "0",
        "_": "minus",
        "+": "equal",
        "{": "bracket_left",
        "}": "bracket_right",
        ":": "semicolon",
        "<": "comma",
        ">": "dot",
        "?": "slash",
        "|": "backslash",
        "~": "grave_accent",
    }
    for ch in text:
        if ch in shift_keymap:
            _send_key(f"shift-{shift_keymap[ch]}", 0.05)
        elif ch in keymap:
            _send_key(keymap[ch], 0.05)
        elif ch.isalpha():
            if ch.isupper():
                _send_key(f"shift-{ch.lower()}", 0.05)
            else:
                _send_key(ch, 0.05)
        else:
            emit("warning", "vm", f"Unmapped character: {ch!r}")


QMP_SOCK = "/tmp/airtag-vm-qmp.sock"
_qmp_lock = threading.Lock()
_qmp_sock = None
_qmp_initialized = False


def _qmp_connect():
    """Connect to QEMU QMP socket and negotiate capabilities."""
    global _qmp_sock, _qmp_initialized
    if _qmp_sock is not None and _qmp_initialized:
        return _qmp_sock
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(QMP_SOCK)
        s.recv(4096)  # QMP greeting
        # Send capabilities negotiation
        s.sendall(b'{"execute": "qmp_capabilities"}\n')
        s.recv(4096)  # response
        _qmp_sock = s
        _qmp_initialized = True
        return s
    except Exception as e:
        emit("error", "vm", f"QMP connect failed: {e}")
        _qmp_sock = None
        _qmp_initialized = False
        return None


def _qmp_disconnect():
    global _qmp_sock, _qmp_initialized
    if _qmp_sock:
        with contextlib.suppress(Exception):
            _qmp_sock.close()
    _qmp_sock = None
    _qmp_initialized = False


def _qmp_cmd(cmd_dict):
    """Send a QMP command and return the response."""
    with _qmp_lock:
        s = _qmp_connect()
        if not s:
            return None
        try:
            s.sendall((json.dumps(cmd_dict) + "\n").encode())
            resp = s.recv(4096).decode(errors="replace")
            return json.loads(resp)
        except Exception as e:
            emit("error", "vm", f"QMP command failed: {e}")
            _qmp_disconnect()
            return None


def _mouse_click(px, py, delay=0.5):
    """Click at pixel coordinates (1280x800 screen) via QMP absolute mouse input."""
    emit("info", "vm", f"Click: ({px}, {py})")
    qx = int((px / 1280) * 32767)
    qy = int((py / 800) * 32767)
    # Move to position and click
    _qmp_cmd(
        {
            "execute": "input-send-event",
            "arguments": {
                "events": [
                    {"type": "abs", "data": {"axis": "x", "value": qx}},
                    {"type": "abs", "data": {"axis": "y", "value": qy}},
                ]
            },
        }
    )
    time.sleep(0.1)
    _qmp_cmd(
        {
            "execute": "input-send-event",
            "arguments": {
                "events": [
                    {"type": "btn", "data": {"down": True, "button": "left"}},
                ]
            },
        }
    )
    time.sleep(0.05)
    _qmp_cmd(
        {
            "execute": "input-send-event",
            "arguments": {
                "events": [
                    {"type": "btn", "data": {"down": False, "button": "left"}},
                ]
            },
        }
    )
    time.sleep(delay)


def _take_screenshot():
    """Take a screenshot via QEMU monitor, return raw PPM bytes or None."""
    ppm_path = "/tmp/airtag-vm-screen.ppm"
    _monitor_cmd(f"screendump {ppm_path}")
    time.sleep(0.3)
    try:
        with open(ppm_path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _ppm_pixel(data, x, y):
    """Read (R, G, B) from raw PPM (P6) data at pixel (x, y)."""
    # Parse P6 header: "P6\n<width> <height>\n<maxval>\n"
    if not data or not data.startswith(b"P6"):
        return (0, 0, 0)
    header_end = 0
    newlines = 0
    for i, b in enumerate(data):
        if b == ord("\n"):
            newlines += 1
            if newlines == 3:
                header_end = i + 1
                break
    # Parse width/height from header
    header = data[:header_end].decode("ascii", errors="replace")
    lines = header.strip().split("\n")
    w, h = map(int, lines[1].split())
    if x < 0 or x >= w or y < 0 or y >= h:
        return (0, 0, 0)
    offset = header_end + (y * w + x) * 3
    return (data[offset], data[offset + 1], data[offset + 2])


def _pixel_brightness(data, x, y):
    r, g, b = _ppm_pixel(data, x, y)
    return r + g + b


def _region_avg_brightness(data, x1, y1, x2, y2, step=10):
    """Average brightness of a rectangular region (sampled)."""
    total, count = 0, 0
    for y in range(y1, y2, step):
        for x in range(x1, x2, step):
            total += _pixel_brightness(data, x, y)
            count += 1
    return total / max(count, 1)


def _ppm_to_image(ppm_data):
    """Convert raw PPM bytes to a PIL Image."""
    if not ppm_data:
        return None
    try:
        return Image.open(io.BytesIO(ppm_data))
    except Exception:
        return None


def _ocr_region(image, x1, y1, x2, y2):
    """Run OCR on a cropped region of a PIL Image. Returns lowercase text."""
    if not image:
        return ""
    try:
        region = image.crop((x1, y1, x2, y2))
        text = pytesseract.image_to_string(region, config="--psm 6")
        return text.lower().strip()
    except Exception as e:
        emit("warning", "vm", f"OCR failed: {e}")
        return ""


def _detect_screen(ppm_data):
    """Analyze screenshot via OCR to determine current VM screen state.
    Returns: 'boot_picker', 'apple_logo', 'recovery', 'terminal', 'setup_wizard',
             'desktop', 'login_screen', 'uefi_error', or 'unknown'.
    Screen is 1280x800.
    """
    if not ppm_data:
        return "unknown"

    # Fast brightness check first — black/dark screens don't need OCR
    center_brightness = _pixel_brightness(ppm_data, 640, 400)
    icon_area_brightness = _region_avg_brightness(ppm_data, 450, 280, 800, 450)
    menubar_brightness = _region_avg_brightness(ppm_data, 50, 2, 400, 22)

    # Mostly-black screen: apple logo or boot picker or unknown
    if menubar_brightness < 50 and icon_area_brightness < 50:
        if center_brightness > 500:
            return "apple_logo"
        return "unknown"

    # Boot picker: dark background but bright icons in center area.
    # Distinguish from UEFI error (also dark+bright but has lots of text).
    if menubar_brightness < 50 and icon_area_brightness > 100:
        img = _ppm_to_image(ppm_data)
        if img:
            text = _ocr_region(img, 200, 400, 1100, 700)
            # UEFI error screens have 300+ chars of "failed to load" text.
            # Boot picker has short labels or OCR noise (< 50 chars).
            # Loading spinner has nearly no text.
            if len(text) < 50:
                return "boot_picker"
            boot_words = ["boot", "opencore", "bas", "system", "macin", "machi",
                          "nvram", "efi", "efl"]
            if any(kw in text for kw in boot_words):
                return "boot_picker"
        return "unknown"

    # Screen has content — use OCR to identify it
    img = _ppm_to_image(ppm_data)
    if not img:
        return "unknown"

    # OCR the full screen (excluding extreme edges)
    text = _ocr_region(img, 50, 0, 1230, 780)
    emit("info", "vm", f"Screen detect OCR ({len(text)} chars): {text[:120]!r}")

    # macOS desktop — Finder menubar is visible
    desktop_keywords = ["finder", "file  edit  view", "go  window  help"]
    if sum(1 for kw in desktop_keywords if kw in text) >= 1:
        return "desktop"

    # macOS login screen
    login_keywords = ["enter password", "log in", "other users"]
    if sum(1 for kw in login_keywords if kw in text) >= 1:
        return "login_screen"

    # Setup wizard keywords — distinctive phrases that only appear in the setup wizard
    setup_keywords = [
        "select your language",
        "country or region",
        "written and spoken",
        "accessibility features",
        "data & privacy",
        "data and privacy",
        "migration assistant",
        "transfer information",
        "apple id",
        "terms and conditions",
        "create a computer account",
        "full name",
        "enable location",
        "select your time zone",
        "analytics",
        "screen time",
        "choose your look",
        "express set up",
        "set up later",
        "not now",
    ]
    setup_matches = sum(1 for kw in setup_keywords if kw in text)
    if setup_matches >= 1:
        return "setup_wizard"

    # Recovery keywords
    recovery_keywords = [
        "macos recovery",
        "reinstall macos",
        "disk utility",
        "restore from time machine",
        "startup security utility",
    ]
    recovery_matches = sum(1 for kw in recovery_keywords if kw in text)
    if recovery_matches >= 1:
        return "recovery"

    # Terminal — look for shell prompt indicators or command-line text
    terminal_keywords = [
        "bash",
        "-sh",
        "root#",
        "terminal",
        "last login",
        "diskutil",
        "volumes",
        "localhost",
        "macintosh",
    ]
    terminal_matches = sum(1 for kw in terminal_keywords if kw in text)
    if terminal_matches >= 1:
        return "terminal"

    # UEFI firmware error — all boot entries failed
    uefi_keywords = ["failed to load", "no bootable option", "press any key", "tianocore",
                      "pciroot", "bdsdxe"]
    if sum(1 for kw in uefi_keywords if kw in text) >= 2:
        return "uefi_error"

    # Boot picker text (OpenCore) — require 2+ matches to avoid false positives
    # ("base system" can appear in migration transfer screen text)
    boot_keywords = ["boot", "opencore", "base system"]
    boot_matches = sum(1 for kw in boot_keywords if kw in text)
    if boot_matches >= 2:
        return "boot_picker"

    # Bright screen with menubar but no recognized text
    if menubar_brightness > 100:
        if center_brightness > 400:
            return "unknown"  # could be setup wizard loading or OpenCore settings
        return "recovery"  # menubar visible, dark center = likely recovery

    return "unknown"


# --- Auto-install state machine ---
_auto_install_phase = (
    "idle"  # idle, booting, boot_picker, waiting_recovery, formatting, installing, done, error
)
_auto_install_start_time = 0
_auto_install_step_times = {}


def _set_phase(phase, msg=None):
    global _auto_install_phase
    _auto_install_phase = phase
    _auto_install_step_times[phase] = time.time()
    if msg:
        emit("info", "vm", msg)
    emit("info", "vm", f"Auto-install phase: {phase}")


def _wait_for_screen(expected, timeout=300, poll_interval=5, msg=None):
    """Poll screenshots until the expected screen state appears.

    Args:
        expected: screen state string or set of acceptable states
        timeout: max seconds to wait
        poll_interval: seconds between polls
        msg: optional status message prefix for progress updates
    Returns:
        (state, ppm_data) if expected state found, or (last_state, ppm_data) on timeout
    """
    if isinstance(expected, str):
        expected = {expected}
    deadline = time.time() + timeout
    last_state = "unknown"
    last_ppm = None
    polls = 0
    while time.time() < deadline:
        last_ppm = _take_screenshot()
        last_state = _detect_screen(last_ppm)
        if last_state in expected:
            return last_state, last_ppm
        polls += 1
        if msg and polls % 6 == 0:
            remaining = int(deadline - time.time())
            emit("info", "vm", f"{msg} (screen: {last_state}, {remaining}s remaining)")
        time.sleep(poll_interval)
    return last_state, last_ppm


VM_USER = "airtag"
VM_PASSWORD = "airtag"
WIZARD_TIMEOUT = 1200  # 20 min — migration transfer + reboot can take 15+ min


# ── Setup Wizard ─────────────────────────────────────────────────────────
#
# Version-specific screen definitions. Each macOS version has different wizard
# screens. To add a new version, add entries to WIZARD_SCREENS below.

MACOS_VERSION = "catalina"


@dataclass
class WizardScreen:
    """A setup wizard screen: how to identify it and what to do."""

    id: str
    match: list[str]  # All must appear in OCR text to identify this screen
    button: str  # Button text to find and click via OCR
    fallback_pos: tuple[int, int] = (986, 670)  # Click here if OCR can't find button
    confirm_button: str | None = None  # Click after main action (e.g. T&C "Agree" popup)
    confirm_fallback: tuple[int, int] | None = None
    custom_action: Callable | None = None  # Override for non-button screens (account form)

    def matches(self, text: str) -> bool:
        return all(kw in text for kw in self.match)

    def execute(self) -> None:
        if self.custom_action:
            self.custom_action()
            return
        if not _find_and_click(self.button):
            emit("info", "vm", f"  → fallback click at {self.fallback_pos}")
            _mouse_click(self.fallback_pos[0], self.fallback_pos[1], 0.3)
        if self.confirm_button:
            time.sleep(1.5)
            if not _find_and_click(self.confirm_button) and self.confirm_fallback:
                emit("info", "vm", f"  → confirm fallback at {self.confirm_fallback}")
                _mouse_click(self.confirm_fallback[0], self.confirm_fallback[1], 0.3)


WIZARD_SCREENS: dict[str, list[WizardScreen]] = {
    "catalina": [
        WizardScreen("country", ["select your country or region"], "Continue"),
        WizardScreen("language", ["written and spoken"], "Continue"),
        WizardScreen("preferred_languages", ["preferred languages"], "Continue"),
        WizardScreen("input_sources", ["input sources"], "Continue"),
        WizardScreen("dictation", ["dictation"], "Continue"),
        WizardScreen("accessibility", ["accessibility", "features"], "Not Now"),
        WizardScreen("privacy", ["data", "privacy"], "Continue"),
        # Migration from recovery partition breaks Macintosh HD boot.
        # transfer_info MUST come before migration — both screens' OCR text
        # contains "migration assistant", so the more specific match wins.
        WizardScreen(
            "transfer_info",
            ["transfer information to this mac"],
            "",
            custom_action=lambda: _handle_transfer_info(),
        ),
        WizardScreen(
            "migration",
            ["migration assistant"],
            "",
            custom_action=lambda: _skip_migration(),
        ),
        WizardScreen(
            "apple_id", ["sign in with your apple id"], "Set Up Later", fallback_pos=(196, 670)
        ),
        WizardScreen(
            "icloud_signin", ["sign in to icloud"], "Set Up Later", fallback_pos=(196, 670)
        ),
        WizardScreen("skip_confirm", ["skip"], "Skip", fallback_pos=(750, 455)),
        WizardScreen(
            "terms",
            ["terms and conditions"],
            "Agree",
            confirm_button="Agree",
            confirm_fallback=(750, 455),
        ),
        WizardScreen(
            "create_account",
            ["create a computer account"],
            "Continue",
            custom_action=lambda: _fill_account_form(),
        ),
        WizardScreen("express_setup", ["express set up"], "Customize Settings"),
        WizardScreen("location", ["enable location"], "Continue"),
        WizardScreen("timezone", ["time zone"], "Continue"),
        WizardScreen("analytics", ["analytics"], "Continue"),
        WizardScreen("siri", ["siri"], "Continue"),
        WizardScreen("screen_time", ["screen time"], "Set Up Later", fallback_pos=(196, 670)),
        WizardScreen("appearance", ["choose your look"], "Continue"),
        WizardScreen("touch_id", ["touch id"], "Continue"),
        WizardScreen("apple_pay", ["apple pay"], "Set Up Later", fallback_pos=(196, 670)),
    ],
}


def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """High-contrast grayscale for reliable button text detection."""
    gray = img.convert("L")
    lo, hi = gray.getextrema()
    if hi == lo:
        return gray
    scale = 255.0 / (hi - lo)
    return gray.point(lambda p: int((p - lo) * scale))


def _find_button_pos(img: Image.Image, label: str, min_y: int = 600) -> tuple[int, int] | None:
    """Find a button's center coordinates by its text label via OCR.

    Args:
        img: Screenshot image to search.
        label: Text label to find (e.g. "Continue", "Back").
        min_y: Only consider text at or below this y coordinate. Default 600
               restricts to the bottom button area. Use 0 to search full screen.
    """
    processed = _preprocess_for_ocr(img)
    try:
        data = pytesseract.image_to_data(processed, output_type=pytesseract.Output.DICT)
    except Exception as e:
        emit("warning", "vm", f"OCR image_to_data failed: {e}")
        return None

    words = label.lower().split()
    for i, raw in enumerate(data["text"]):
        if not raw.strip():
            continue
        if data["top"][i] < min_y:
            continue
        if words[0] not in raw.strip().lower():
            continue
        if len(words) == 1:
            return (
                data["left"][i] + data["width"][i] // 2,
                data["top"][i] + data["height"][i] // 2,
            )
        matched = all(
            i + j < len(data["text"]) and part in data["text"][i + j].strip().lower()
            for j, part in enumerate(words[1:], 1)
        )
        if matched:
            last = i + len(words) - 1
            x = (data["left"][i] + data["left"][last] + data["width"][last]) // 2
            y = data["top"][i] + data["height"][i] // 2
            return (x, y)
    return None


def _find_and_click(label: str) -> bool:
    """Screenshot → find button by OCR → click it. Retries once on failure."""
    for attempt in range(2):
        if attempt > 0:
            time.sleep(1)
        ppm = _take_screenshot()
        img = _ppm_to_image(ppm)
        if not img:
            continue
        pos = _find_button_pos(img, label)
        if pos:
            emit("info", "vm", f"  → '{label}' at ({pos[0]}, {pos[1]})")
            _mouse_click(pos[0], pos[1], 0.3)
            return True
    emit("warning", "vm", f"Button '{label}' not found after 2 attempts")
    return False


def _try_dont_transfer(img) -> bool:
    """Try to find and click 'Don't transfer' on the migration intro screen.

    Returns True if found and clicked.
    """
    if not img:
        return False

    # OCR search across the full screen
    for label in ["Don't transfer", "not transfer", "don't transfer"]:
        pos = _find_button_pos(img, label, min_y=0)
        if pos:
            emit("info", "vm", f"  → Found '{label}' at {pos}, clicking")
            _mouse_click(pos[0], pos[1], 0.5)
            return True
    return False


def _skip_migration() -> None:
    """Select 'Don't transfer any information now' on the Migration Assistant screen.

    The migration intro screen may auto-advance to transfer_info within ~1s,
    so this must act fast. Saves full OCR for debugging if the radio isn't found.
    """
    _skip_migration.attempts = getattr(_skip_migration, "attempts", 0) + 1
    attempt = _skip_migration.attempts

    ppm = _take_screenshot()
    img = _ppm_to_image(ppm) if ppm else None

    # Save screenshot + full OCR for debugging
    if img:
        img.save(f"/tmp/airtag-vm-migration-intro-{attempt}.png")
        text = _ocr_region(img, 50, 50, 1230, 750)
        emit("info", "vm", f"  → Migration intro OCR ({len(text)} chars): {text[:300]!r}")

    if _try_dont_transfer(img):
        time.sleep(0.5)
        if not _find_and_click("Continue"):
            _mouse_click(959, 665, 0.3)
        time.sleep(2)
        return

    # Fallback: try different y positions for the 3rd radio button
    y_positions = [500, 480, 520, 460, 540]
    y = y_positions[(attempt - 1) % len(y_positions)]
    emit("info", "vm", f"  → OCR miss, trying radio position (380, {y}) [attempt {attempt}]")
    _mouse_click(380, y, 0.5)
    time.sleep(0.5)
    if not _find_and_click("Continue"):
        _mouse_click(959, 665, 0.3)
    time.sleep(2)


def _handle_transfer_info() -> None:
    """Handle 'Transfer Information to This Mac' screen.

    The migration intro screen auto-advances here in ~1s. Strategy:
    1. Click Back to return to migration intro
    2. Immediately (before auto-advance) find and click 'Don't transfer'
    3. Then click Continue to skip migration entirely
    """
    _handle_transfer_info.attempts = getattr(_handle_transfer_info, "attempts", 0) + 1
    attempt = _handle_transfer_info.attempts

    # Save debug screenshot of transfer_info
    ppm = _take_screenshot()
    if ppm:
        try:
            img = _ppm_to_image(ppm)
            if img:
                img.save(f"/tmp/airtag-vm-transfer-info-{attempt}.png")
        except Exception:
            pass

    # Step 1: Click Back to return to migration intro
    emit("info", "vm", f"  → transfer_info: clicking Back (attempt {attempt})")
    if not _find_and_click("Back"):
        _mouse_click(196, 665, 0.3)  # Back button bottom-left
    time.sleep(1.5)  # Wait for migration intro to appear

    # Step 2: Immediately capture and interact with migration intro
    ppm = _take_screenshot()
    img = _ppm_to_image(ppm) if ppm else None

    if img:
        img.save(f"/tmp/airtag-vm-after-back-{attempt}.png")
        text = _ocr_region(img, 50, 50, 1230, 750)
        emit("info", "vm", f"  → After Back OCR ({len(text)} chars): {text[:300]!r}")

    # Step 3: Try to find "Don't transfer" on migration intro
    if _try_dont_transfer(img):
        time.sleep(0.5)
        if not _find_and_click("Continue"):
            _mouse_click(959, 665, 0.3)
        time.sleep(2)
        return

    # Step 4: Fallback — try radio button positions
    y_positions = [500, 480, 520, 460, 540]
    y = y_positions[(attempt - 1) % len(y_positions)]
    emit("info", "vm", f"  → Fallback radio position (380, {y}) [attempt {attempt}]")
    _mouse_click(380, y, 0.5)
    time.sleep(0.5)
    if not _find_and_click("Continue"):
        _mouse_click(959, 665, 0.3)
    time.sleep(2)


def _fill_account_form() -> None:
    """Fill the Create a Computer Account form."""
    emit("info", "vm", "  → Filling account form")
    ppm = _take_screenshot()
    img = _ppm_to_image(ppm)
    pos = _find_button_pos(img, "Full Name", min_y=0) if img else None
    _mouse_click(pos[0] + 200, pos[1], 0.3) if pos else _mouse_click(790, 330, 0.3)
    _type_text(VM_USER)
    _send_key("tab", 0.2)
    _send_key("tab", 0.2)
    _type_text(VM_PASSWORD)
    _send_key("tab", 0.2)
    _type_text(VM_PASSWORD)
    _send_key("tab", 0.2)
    time.sleep(0.3)
    _find_and_click("Continue")
    time.sleep(3)


def _wizard_ocr(ppm: bytes) -> str:
    """OCR the screen for wizard identification."""
    img = _ppm_to_image(ppm)
    if not img:
        return ""
    return _ocr_region(img, 50, 50, 1230, 750)


def _wizard_identify(text: str) -> WizardScreen | None:
    """Match OCR text against known screens for the current macOS version."""
    for screen in WIZARD_SCREENS[MACOS_VERSION]:
        if screen.matches(text):
            return screen
    return None


def _run_setup_wizard():
    """Walk through macOS setup wizard using version-specific screen definitions."""
    _set_phase("setup_wizard", "Automating setup wizard...")
    screens = WIZARD_SCREENS[MACOS_VERSION]
    emit("info", "vm", f"Using {MACOS_VERSION} definitions ({len(screens)} screens)")

    try:
        time.sleep(3)
        last_id = None
        stuck = 0
        deadline = time.time() + WIZARD_TIMEOUT

        while time.time() < deadline:
            ppm = _take_screenshot()
            screen_type = _detect_screen(ppm)

            # Desktop or login screen = wizard is done
            if screen_type == "desktop":
                emit("info", "vm", "macOS desktop detected — wizard complete.")
                break
            if screen_type == "login_screen":
                emit("info", "vm", "macOS login screen detected — wizard complete.")
                break

            # Boot picker — press Enter on default entry (Macintosh HD)
            if screen_type == "boot_picker":
                emit("info", "vm", "Wizard: boot picker detected, pressing Enter...")
                _send_key("ret", 2)
                state, ppm = _wait_for_screen(
                    {"setup_wizard", "apple_logo", "desktop", "login_screen"},
                    timeout=120,
                    poll_interval=5,
                    msg="Waiting for macOS to boot",
                )
                if state in ("desktop", "login_screen"):
                    emit("info", "vm", f"macOS {state} detected — wizard complete.")
                    break
                if state == "setup_wizard":
                    continue
                # apple_logo or timeout — fall through

            if screen_type not in ("setup_wizard", "unknown", "apple_logo", "boot_picker"):
                emit("info", "vm", f"Left wizard (screen: {screen_type}).")
                break

            if screen_type in ("unknown", "apple_logo"):
                state, ppm = _wait_for_screen(
                    {"setup_wizard", "boot_picker", "desktop", "login_screen"},
                    timeout=180,
                    poll_interval=5,
                    msg="Waiting for wizard screen",
                )
                if state in ("desktop", "login_screen"):
                    emit("info", "vm", f"macOS {state} detected — wizard complete.")
                    break
                if state == "boot_picker":
                    continue  # handle boot picker at top of loop
                if state != "setup_wizard":
                    emit("info", "vm", f"Wizard ended (screen: {state}).")
                    break

            text = _wizard_ocr(ppm)
            if not text:
                time.sleep(1)
                continue

            screen = _wizard_identify(text)
            if not screen:
                emit("info", "vm", f"Unknown screen: {text[:80]!r}")
                _find_and_click("Continue")
                time.sleep(1)
                continue

            if screen.id == last_id:
                stuck += 1
                if stuck >= 3:
                    emit("info", "vm", f"Stuck on '{screen.id}', trying Tab+Enter")
                    _send_key("tab", 0.3)
                    _send_key("ret", 1)
                    time.sleep(1)
                    continue
            else:
                stuck = 0

            last_id = screen.id
            emit("info", "vm", f"Screen: {screen.id} → '{screen.button}'")
            screen.execute()
            time.sleep(1)

        pw_path = DATA_DIR / "vm-password"
        pw_path.write_text(VM_PASSWORD)
        pw_path.chmod(0o600)
        _set_phase("done", "macOS setup complete!")
        emit("info", "vm", f"Account: {VM_USER} / {VM_PASSWORD}")

    except Exception as e:
        _set_phase("error", f"Setup wizard failed: {e}")
        log.exception("Setup wizard error")


def _auto_install_worker():
    """Autonomous macOS install via Terminal + startosinstall.

    Flow: boot picker → recovery → Terminal → format disk → startosinstall.
    All coordinates validated on 1280x800 screen.
    Every step verifies screen state before and after acting — no blind waits.
    """
    global _auto_install_start_time
    _auto_install_start_time = time.time()

    try:
        _set_phase("booting", "Starting macOS VM and auto-install...")

        # Wait for QEMU monitor to become responsive
        for _attempt in range(120):
            if os.path.exists(MONITOR_SOCK):
                resp = _monitor_cmd("info status")
                if resp and "running" in resp:
                    break
            time.sleep(5)
        else:
            _set_phase("error", "VM failed to start — monitor not responding")
            return

        # === STEP 1: Wait for boot picker ===
        _set_phase("boot_picker", "VM booted, waiting for boot picker...")
        state, _ = _wait_for_screen(
            {"boot_picker", "recovery", "setup_wizard"},
            timeout=180,
            poll_interval=5,
            msg="Waiting for boot picker",
        )

        # macOS already installed — setup wizard is showing
        if state == "setup_wizard":
            _run_setup_wizard()
            return

        if state == "boot_picker":
            emit("info", "vm", "Boot picker detected, selecting macOS Base System...")
            # OpenCore shows ~3 entries: EFI, macOS Base System, Macintosh HD.
            # Default selection is leftmost (EFI). One right → Base System.
            _send_key("right", 0.5)
            _send_key("ret", 1)

            # After pressing enter, OpenCore shows a loading screen (dark bg +
            # bright spinner) that the brightness heuristic misclassifies as
            # boot_picker. Accept boot_picker too and just wait long enough for
            # recovery/apple_logo to eventually appear.
            state, _ = _wait_for_screen(
                {"recovery", "apple_logo", "setup_wizard"},
                timeout=180,
                poll_interval=5,
                msg="Waiting for boot to proceed",
            )
            if state == "setup_wizard":
                _run_setup_wizard()
                return
        elif state != "recovery":
            _set_phase("error", f"Expected boot picker or recovery, got: {state}")
            return

        # === STEP 2: Wait for recovery ===
        if state != "recovery":
            _set_phase("boot_picker", "Booting into recovery...")
            state, _ = _wait_for_screen(
                "recovery", timeout=300, poll_interval=5, msg="Waiting for recovery"
            )
            if state != "recovery":
                if state == "apple_logo":
                    # Apple logo persisting = likely a resumed/ongoing macOS installation
                    # Skip straight to install monitoring
                    _set_phase(
                        "installing",
                        "macOS installation already in progress (resumed from previous attempt)...",
                    )
                    emit(
                        "info",
                        "vm",
                        "Apple logo with progress bar detected — monitoring ongoing installation...",
                    )
                    install_start = time.time() - 600  # assume started ~10 min ago
                    last_screen = "apple_logo"
                    # Jump to install monitoring (same loop as STEP 6)
                    for poll in range(360):
                        time.sleep(10)
                        ppm = _take_screenshot()
                        screen = _detect_screen(ppm)
                        elapsed_min = int((time.time() - install_start) / 60)
                        if screen != last_screen:
                            emit(
                                "info",
                                "vm",
                                f"Screen changed: {last_screen} → {screen} ({elapsed_min} min)",
                            )
                            last_screen = screen
                        if screen == "boot_picker":
                            emit(
                                "info",
                                "vm",
                                "VM rebooted to boot picker, selecting first entry...",
                            )
                            _send_key("ret", 1)
                        elif screen == "setup_wizard":
                            _set_phase(
                                "done",
                                "macOS is installed! Setup wizard is ready in VNC.",
                            )
                            return
                        elif screen == "apple_logo":
                            if poll % 6 == 0:
                                emit(
                                    "info",
                                    "vm",
                                    f"macOS installing... ({elapsed_min} min)",
                                )
                        elif screen == "recovery":
                            if elapsed_min > 45:
                                _set_phase(
                                    "done",
                                    "macOS may be installed. Check VNC for current state.",
                                )
                                return
                        elif screen == "unknown" and poll % 12 == 0:
                            emit("info", "vm", f"VM rebooting... ({elapsed_min} min)")
                        if time.time() - install_start > 4200:  # 70 min
                            _set_phase(
                                "done",
                                "Installation monitoring timed out. Check VNC for current state.",
                            )
                            return
                    _set_phase("done", "Auto-install monitoring complete. Check VNC.")
                    return
                _set_phase("error", f"Recovery not reached after 5 min (screen: {state})")
                return

        recovery_time = time.time() - _auto_install_start_time
        _set_phase(
            "formatting",
            f"Recovery loaded after {int(recovery_time)}s. Opening Terminal...",
        )

        # Let recovery GUI fully render
        for _ in range(6):
            time.sleep(2)
            s, _ = _wait_for_screen({"recovery", "setup_wizard"}, timeout=5, poll_interval=1)
            if s == "setup_wizard":
                _run_setup_wizard()
                return
            if s == "recovery":
                break

        # === STEP 3: Open Terminal via Utilities menu ===
        # Menu bar items at y=10: [Apple x=20] [Recovery x=83] [File x=144] [Edit x=187] [Utilities x=242] [Window x=309]
        emit("info", "vm", "Opening Terminal via Utilities menu...")
        _mouse_click(242, 10, 1.5)  # Click "Utilities" in menu bar
        _mouse_click(242, 55, 4)  # Click "Terminal" (2nd dropdown item)

        # Verify Terminal opened — poll for "terminal" screen state (red traffic light button)
        time.sleep(1)
        _mouse_click(400, 300, 1)  # Click Terminal window to focus
        state, _ = _wait_for_screen("terminal", timeout=15, poll_interval=2)

        if state != "terminal":
            # Terminal may have opened but traffic light not detected — try one more time
            emit(
                "info",
                "vm",
                f"Terminal check: got '{state}', retrying Utilities → Terminal...",
            )
            _send_key("escape", 0.5)
            time.sleep(1)
            _mouse_click(242, 10, 1.5)
            _mouse_click(242, 55, 4)
            time.sleep(2)
            _mouse_click(400, 300, 1)
            state, _ = _wait_for_screen("terminal", timeout=10, poll_interval=2)
            if state == "setup_wizard":
                emit(
                    "info",
                    "vm",
                    "Setup wizard detected instead of recovery Terminal — macOS is already installed.",
                )
                _run_setup_wizard()
                return
            if state != "terminal":
                emit(
                    "warning", "vm",
                    f"Terminal detection returned '{state}' — proceeding anyway (detection may be imprecise)",
                )

        emit("info", "vm", "Terminal is open.")

        # === STEP 4: Format disk ===
        # First, list all disks so we can see the layout and find the 80GB disk.
        emit("info", "vm", "Listing disks...")
        _type_text("diskutil list")
        _send_key("ret")
        time.sleep(5)

        # Save screenshot of disk list for debugging
        ppm = _take_screenshot()
        if ppm:
            try:
                img = _ppm_to_image(ppm)
                if img:
                    img.save("/tmp/airtag-vm-disklist.png")
                    text = _ocr_region(img, 50, 50, 1230, 750)
                    emit("info", "vm", f"Disk list OCR: {text[:500]!r}")
            except Exception:
                pass

        # QEMU attaches: sata.2=OpenCore, sata.3=BaseSystem, sata.4=MacHDD(80GB).
        # macOS enumerates: disk0=sata.2, disk1=sata.3, disk2=sata.4.
        # Format the 80GB disk (disk2 when BaseSystem present, disk1 without).
        emit("info", "vm", "Formatting disk2 (80GB main disk, GUID + APFS)...")
        _type_text('diskutil eraseDisk APFS "Macintosh HD" GPT disk2')
        _send_key("ret")

        # Poll until format completes — we can't read terminal text, but format takes ~15-30s.
        # Poll for terminal still being visible (ensures VM hasn't crashed/rebooted).
        format_start = time.time()
        for i in range(20):
            time.sleep(3)
            ppm = _take_screenshot()
            s = _detect_screen(ppm)
            elapsed_fmt = int(time.time() - format_start)
            if s not in ("terminal", "recovery"):
                emit("warning", "vm", f"Unexpected screen during format: {s}")
                break
            if elapsed_fmt >= 30:
                break
            if i % 3 == 0:
                emit("info", "vm", f"Formatting... ({elapsed_fmt}s)")

        # Verify we're still in terminal after format
        state, _ = _wait_for_screen({"terminal", "recovery"}, timeout=10, poll_interval=2)
        emit("info", "vm", f"Format complete (screen: {state}).")

        # === STEP 5: Find and run startosinstall ===
        _set_phase("installing", "Starting macOS installer from command line...")
        emit("info", "vm", "Finding macOS installer (startosinstall)...")

        _type_text(
            'INST=$(find / -name startosinstall -maxdepth 6 2>/dev/null | head -1); echo "FOUND:$INST"'
        )
        _send_key("ret")

        # Wait for find to complete — poll terminal state to ensure VM is responsive
        find_start = time.time()
        for _ in range(10):
            time.sleep(2)
            ppm = _take_screenshot()
            s = _detect_screen(ppm)
            if s not in ("terminal", "recovery"):
                emit("warning", "vm", f"Unexpected screen during find: {s}")
                break
            if time.time() - find_start >= 10:
                break

        emit("info", "vm", "Running startosinstall --agreetolicense...")
        _type_text('"$INST" --agreetolicense --volume "/Volumes/Macintosh HD"')
        _send_key("ret")

        # Wait for startosinstall to show output, then save debug screenshot
        time.sleep(15)
        ppm = _take_screenshot()
        if ppm:
            try:
                img = _ppm_to_image(ppm)
                if img:
                    img.save("/tmp/airtag-vm-startosinstall.png")
                    text = _ocr_region(img, 50, 50, 1230, 750)
                    emit("info", "vm", f"startosinstall screen: {text[:500]!r}")
            except Exception:
                pass

        _set_phase("installing", "macOS installation in progress. This takes 30-60 minutes...")
        emit(
            "info",
            "vm",
            "startosinstall is preparing the installation. The VM will reboot when ready...",
        )

        # === STEP 6: Monitor installation progress ===
        # startosinstall prepares in Terminal, then triggers a reboot.
        # After reboot: boot picker → apple logo → potentially more reboots → setup wizard.
        install_start = time.time()
        last_screen = "terminal"

        for poll in range(360):  # up to 60 min at 10s intervals
            time.sleep(10)
            ppm = _take_screenshot()
            screen = _detect_screen(ppm)
            elapsed_min = int((time.time() - install_start) / 60)

            # Log state transitions
            if screen != last_screen:
                emit(
                    "info",
                    "vm",
                    f"Screen changed: {last_screen} → {screen} ({elapsed_min} min)",
                )
                last_screen = screen

            if screen == "boot_picker":
                # After startosinstall, the first boot entry should be "macOS Installer"
                # or the installed macOS. Just press Enter to boot the default/first entry.
                # Do NOT navigate right — that would select the recovery BaseSystem.
                emit("info", "vm", "VM rebooted to boot picker, selecting first entry...")
                _send_key("ret", 1)

            elif screen in ("terminal", "recovery") and elapsed_min < 45:
                # startosinstall still preparing in Terminal, or intermediate recovery boot
                if poll % 6 == 0:
                    emit("info", "vm", f"Installer preparing... ({elapsed_min} min)")

            elif screen == "apple_logo":
                if poll % 6 == 0:
                    emit("info", "vm", f"macOS installing... ({elapsed_min} min)")

            elif screen == "setup_wizard":
                _run_setup_wizard()
                return

            elif screen == "uefi_error":
                # UEFI firmware is trying boot entries (PXE, HTTP, etc). This takes
                # minutes. Only press a key once "press any key" or "no bootable"
                # appears — before that, the firmware is still enumerating.
                img = _ppm_to_image(ppm)
                full_text = _ocr_region(img, 0, 0, 1280, 800) if img else ""
                if "press any key" in full_text or "no bootable" in full_text:
                    emit("info", "vm", f"UEFI done enumerating, pressing key for Boot Manager... ({elapsed_min} min)")
                    _send_key("ret", 3)
                    # Boot Manager shows a list — first entry should be OpenCore.
                    _send_key("ret", 1)
                elif poll % 6 == 0:
                    emit("info", "vm", f"UEFI enumerating boot entries... ({elapsed_min} min)")

            elif screen == "unknown":
                # Black screen during reboot — normal
                if poll % 12 == 0:
                    emit("info", "vm", f"VM rebooting... ({elapsed_min} min)")

            elif screen == "recovery" and elapsed_min >= 45:
                # Recovery screen after a very long install = something may be wrong
                _set_phase("done", "macOS may be installed. Check VNC for current state.")
                return

            # Timeout after 60 min
            if time.time() - install_start > 3600:
                _set_phase(
                    "done",
                    "Installation monitoring timed out after 60 min. Check VNC for current state.",
                )
                return

        _set_phase("done", "Auto-install monitoring complete. Check VNC for current state.")

    except Exception as e:
        _set_phase("error", f"Auto-install failed: {e}")
        log.exception("Auto-install error")


@app.route("/api/vm/install-status")
def vm_install_status():
    """Return current auto-install phase and timing info."""
    elapsed = time.time() - _auto_install_start_time if _auto_install_start_time else 0
    return jsonify(
        {
            "phase": _auto_install_phase,
            "elapsed_seconds": int(elapsed),
            "step_times": {
                k: int(v - _auto_install_start_time) for k, v in _auto_install_step_times.items()
            },
        }
    )


@app.route("/api/vm/complete-setup", methods=["POST"])
def vm_complete_setup():
    """Mark VM setup as complete. Saves VM password and stops the VM."""
    data = request.get_json() or {}
    password = data.get("password")

    pw_path = DATA_DIR / "vm-password"
    if password:
        emit("info", "vm", "VM setup marked complete, saving password")
        pw_path.write_text(password)
        pw_path.chmod(0o600)
    elif not pw_path.exists():
        return jsonify({"error": "VM user password required"}), 400

    vm_stop()

    # Remove installer media so future boots skip straight to macOS
    for f in ["BaseSystem.img", "BaseSystem.dmg"]:
        p = VM_DIR / f
        if p.exists():
            p.unlink()
            emit("info", "vm", f"Removed installer media ({f})")
    recovery = VM_DIR / "com.apple.recovery.boot"
    if recovery.exists():
        import shutil

        shutil.rmtree(recovery, ignore_errors=True)
        emit("info", "vm", "Removed recovery boot files")

    emit("info", "vm", "Triggering first key extraction")
    _systemctl("start", "airtag-extract-keys")
    threading.Thread(target=_tail_journal, args=("airtag-extract-keys", "vm"), daemon=True).start()

    return jsonify({"status": "complete"})


@app.route("/api/vm/reinstall", methods=["POST"])
def vm_reinstall():
    """Wipe the macOS VM and reprovision from scratch."""
    if not VM_ENABLED:
        return jsonify({"error": "VM not enabled"}), 400

    emit("info", "vm", "Reinstalling macOS VM — wiping disk and reprovisioning")

    # Stop VM if running
    vm_stop()

    # Remove disk image and password — keep installer media (BaseSystem) for reuse
    p = VM_DIR / "mac_hdd_ng.img"
    if p.exists():
        p.unlink()
    pw_path = DATA_DIR / "vm-password"
    if pw_path.exists():
        pw_path.unlink()

    emit("info", "vm", "Disk wiped, starting reprovisioning")

    def _reinstall_worker():
        """Provision VM, then auto-start setup."""
        try:
            result = sp.run(
                [
                    "/run/wrappers/bin/sudo",
                    "/run/current-system/sw/bin/systemctl",
                    "restart",
                    "airtag-provision-vm",
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                emit("info", "vm", "Provisioning complete, auto-starting VM setup")
                # Call vm_start_setup logic directly
                with app.test_request_context():
                    vm_start_setup()
            else:
                emit("error", "vm", f"Provisioning failed: {result.stderr.strip()}")
        except Exception as e:
            emit("error", "vm", f"Reinstall worker error: {e}")

    threading.Thread(target=_reinstall_worker, daemon=True).start()
    threading.Thread(target=_tail_journal, args=("airtag-provision-vm", "vm"), daemon=True).start()

    return jsonify({"status": "reprovisioning"})


@app.route("/api/account/status")
def account_status():
    """Check if Apple account is configured and session is valid."""
    return jsonify(
        {
            "configured": ACCOUNT_PATH.exists(),
            "airtags": len(list(KEYS_DIR.glob("*.json"))) if KEYS_DIR.exists() else 0,
        }
    )


@app.route("/api/account/login", methods=["POST"])
def account_login():
    """Start Apple ID login. Returns 2FA methods if needed."""
    global _pending_account, _pending_2fa_methods
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"error": "email and password required"}), 400

    try:
        emit("info", "account", f"Logging in as {email}")
        ani = LocalAnisetteProvider(libs_path=str(ANISETTE_PATH))
        acc = AppleAccount(ani)
        state = acc.login(email, password)

        if state == LoginState.REQUIRE_2FA:
            _pending_account = acc
            methods = acc.get_2fa_methods()
            _pending_2fa_methods = methods
            if methods:
                methods[0].request()
            method_list = []
            for m in methods:
                if isinstance(m, SyncSmsSecondFactor):
                    method_list.append(
                        {
                            "type": "sms",
                            "phone": m.phone_number,
                            "id": m.phone_number_id,
                        }
                    )
                else:
                    method_list.append({"type": "trusted_device"})
            emit("info", "account", f"2FA required ({len(methods)} method(s) available)")
            return jsonify({"status": "2fa_required", "methods": method_list})

        if state == LoginState.LOGGED_IN:
            acc.to_json(str(ACCOUNT_PATH))
            _pending_account = None
            _pending_2fa_methods = None
            emit("info", "account", "Logged in successfully (no 2FA needed)")
            return jsonify({"status": "logged_in"})

        emit("error", "account", f"Unexpected login state: {state}")
        return jsonify({"error": f"Unexpected login state: {state}"}), 500
    except Exception as e:
        _pending_account = None
        _pending_2fa_methods = None
        emit("error", "account", f"Login failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/account/2fa", methods=["POST"])
def account_2fa():
    """Submit 2FA code to complete login."""
    global _pending_account, _pending_2fa_methods
    if not _pending_account or not _pending_2fa_methods:
        return jsonify({"error": "No pending login. Call /api/account/login first."}), 400

    data = request.get_json()
    code = data.get("code")
    method_index = data.get("method", 0)

    if not code:
        return jsonify({"error": "code required"}), 400

    try:
        method = _pending_2fa_methods[method_index]
        state = method.submit(code)

        if state == LoginState.LOGGED_IN:
            _pending_account.to_json(str(ACCOUNT_PATH))
            _pending_account = None
            _pending_2fa_methods = None
            emit("info", "account", "2FA verified, logged in successfully")
            return jsonify({"status": "logged_in"})

        emit("warning", "account", f"2FA rejected (state: {state})")
        return jsonify({"error": f"2FA failed, state: {state}"}), 401
    except Exception as e:
        emit("error", "account", f"2FA error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/account/2fa/request", methods=["POST"])
def account_2fa_request():
    """Request a 2FA code to be sent (for SMS methods)."""
    global _pending_2fa_methods
    if not _pending_2fa_methods:
        return jsonify({"error": "No pending login."}), 400

    data = request.get_json() or {}
    method_index = data.get("method", 0)

    try:
        method = _pending_2fa_methods[method_index]
        method.request()
        return jsonify({"status": "sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys/upload", methods=["POST"])
def upload_keys():
    """Upload AirTag key files (JSON format from FindMy.py or plist_to_findmy.py)."""
    if "file" in request.files:
        f = request.files["file"]
        if not f.filename.endswith(".json"):
            return jsonify({"error": "Only .json files accepted"}), 400
        try:
            data = json.loads(f.read())
            # Validate it's a valid key file
            FindMyAccessory.from_json(data)
            name = data.get("name", f.filename.rsplit(".", 1)[0])
            safe_name = name.replace(" ", "_").replace("/", "_")
            out_path = KEYS_DIR / f"{safe_name}.json"
            with open(out_path, "w") as out:
                json.dump(data, out, indent=2)
            return jsonify({"status": "ok", "name": safe_name})
        except Exception as e:
            return jsonify({"error": f"Invalid key file: {e}"}), 400
    elif request.is_json:
        data = request.get_json()
        try:
            FindMyAccessory.from_json(data)
            name = data.get("name", data.get("identifier", "unknown"))
            safe_name = name.replace(" ", "_").replace("/", "_")
            out_path = KEYS_DIR / f"{safe_name}.json"
            with open(out_path, "w") as out:
                json.dump(data, out, indent=2)
            return jsonify({"status": "ok", "name": safe_name})
        except Exception as e:
            return jsonify({"error": f"Invalid key data: {e}"}), 400
    return jsonify({"error": "Send a JSON file or JSON body"}), 400


@app.route("/api/keys", methods=["GET"])
def list_keys():
    """List all loaded AirTag key files."""
    keys = []
    if KEYS_DIR.exists():
        for f in KEYS_DIR.glob("*.json"):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                keys.append(
                    {
                        "file": f.name,
                        "name": data.get("name", f.stem),
                        "model": data.get("model", "unknown"),
                        "identifier": data.get("identifier", ""),
                    }
                )
            except Exception:
                keys.append({"file": f.name, "name": f.stem, "error": True})
    return jsonify(keys)


@app.route("/api/keys/<name>", methods=["DELETE"])
def delete_key(name):
    """Delete an AirTag key file."""
    path = KEYS_DIR / f"{name}.json"
    if not path.exists():
        return jsonify({"error": "Key not found"}), 404
    path.unlink()
    return jsonify({"status": "deleted"})


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    settings = load_settings()
    emit("info", "system", f"Server starting on port {PORT}")
    emit("info", "system", f"Data dir: {DATA_DIR}")
    emit("info", "system", f"VM enabled: {VM_ENABLED}")
    emit("info", "system", f"Account configured: {ACCOUNT_PATH.exists()}")
    n_keys = len(list(KEYS_DIR.glob("*.json"))) if KEYS_DIR.exists() else 0
    emit("info", "system", f"Loaded {n_keys} AirTag key(s)")
    adaptive = settings.get("adaptive", True)
    emit(
        "info",
        "system",
        f"Polling: idle={settings['idle_interval']}s, active={settings['active_interval']}s, adaptive={'on' if adaptive else 'off'}",
    )

    # Stream VM provisioning logs if it's running
    if VM_ENABLED:
        check = sp.run(
            ["systemctl", "is-active", "airtag-provision-vm"],
            capture_output=True,
            text=True,
        )
        if check.stdout.strip() in ("active", "activating"):
            emit("info", "vm", "VM provisioning is running, streaming logs")
            threading.Thread(
                target=_tail_journal, args=("airtag-provision-vm", "vm"), daemon=True
            ).start()

    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()

    app.run(host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
