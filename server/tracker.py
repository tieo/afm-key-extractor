"""AirTag tracker server — polls Apple's Find My network and serves location history."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import subprocess as sp
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from findmy import AppleAccount, FindMyAccessory, LocalAnisetteProvider
from findmy.reports import LoginState, SyncSmsSecondFactor
from flask import Flask, jsonify, request, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

DATA_DIR = Path(os.environ.get("AIRTAG_DATA_DIR", "/var/lib/airtag-tracker"))
KEYS_DIR = DATA_DIR / "keys"
DB_PATH = DATA_DIR / "locations.db"
ACCOUNT_PATH = DATA_DIR / "account.json"  # legacy plaintext (migrated on load)
ACCOUNT_ENC_PATH = DATA_DIR / "account.enc"
ANISETTE_PATH = DATA_DIR / "ani_libs.bin"
POLL_INTERVAL = int(os.environ.get("AIRTAG_POLL_INTERVAL", "900"))  # 15 min default
PORT = int(os.environ.get("AIRTAG_PORT", "8042"))
STATIC_DIR = Path(__file__).parent / "static"
VM_ENABLED = os.environ.get("AIRTAG_VM_ENABLED", "false") == "true"
VM_DIR = Path(os.environ.get("AIRTAG_VM_DIR", "/var/lib/airtag-tracker/osx-kvm"))
VNC_WS_PORT = int(os.environ.get("AIRTAG_VNC_WS_PORT", "6901"))
VM_PASSWORD = os.environ.get("AIRTAG_VM_PASSWORD", "airtag")

app = Flask(__name__, static_folder=str(STATIC_DIR))


# --- Account-at-rest encryption (Fernet keyed by /etc/machine-id) ---
import base64
import hashlib

from cryptography.fernet import Fernet


def _account_key() -> bytes:
    mid = Path("/etc/machine-id").read_text().strip().encode()
    derived = hashlib.pbkdf2_hmac("sha256", mid, b"airtag-tracker-account-v1", 200_000, dklen=32)
    return base64.urlsafe_b64encode(derived)


_FERNET = Fernet(_account_key())


def account_exists() -> bool:
    return ACCOUNT_ENC_PATH.exists() or ACCOUNT_PATH.exists()


def save_account(acc) -> None:
    payload = json.dumps(acc.to_json()).encode()
    token = _FERNET.encrypt(payload)
    ACCOUNT_ENC_PATH.write_bytes(token)
    ACCOUNT_ENC_PATH.chmod(0o600)


def load_account(anisette):
    if ACCOUNT_ENC_PATH.exists():
        data = json.loads(_FERNET.decrypt(ACCOUNT_ENC_PATH.read_bytes()))
        return AppleAccount.from_json(data, anisette=anisette)
    if ACCOUNT_PATH.exists():
        acc = AppleAccount.from_json(str(ACCOUNT_PATH), anisette=anisette)
        save_account(acc)
        ACCOUNT_PATH.unlink()
        emit("info", "account", "Migrated plaintext account.json → encrypted account.enc")
        return acc
    return None


try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.chmod(0o700)
except Exception:
    pass

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
    if account_exists():
        try:
            acc = load_account(ani)
            if acc is not None:
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

        save_account(acc)
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
    resp = send_from_directory(str(STATIC_DIR), "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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

    # Restore from the pre-baked golden image if one exists so the VM boots
    # straight into the logged-in desktop.
    golden_path = VM_DIR / "mac_hdd_golden.img"
    use_golden = golden_path.exists()
    if use_golden:
        emit("info", "vm", f"Golden image found — restoring {golden_path.name} → mac_hdd_ng.img")
        try:
            import shutil
            shutil.copy2(golden_path, VM_DIR / "mac_hdd_ng.img")
        except Exception as e:
            emit("error", "vm", f"Failed to restore golden image: {e}")
            return jsonify({"error": f"Failed to restore golden image: {e}"}), 500

    emit("info", "vm", f"Starting VM (golden: {use_golden})")

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

    try:
        result = sp.run(qemu_args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            emit("error", "vm", f"QEMU failed to start: {result.stderr}")
            return jsonify({"error": f"Failed to start VM: {result.stderr}"}), 500

        _systemctl("start", "airtag-novnc")
        emit("info", "vm", f"VM started, noVNC proxy active on port {VNC_WS_PORT}")

        # OpenCore's boot-entry NVRAM is wiped on each boot (snapshot=on), so
        # OpenCore shows its picker every time. Send Enter after a short delay
        # to accept the default entry and boot macOS without operator input.
        def _auto_boot_opencore():
            import threading, time as _t
            def worker():
                _t.sleep(6)
                try:
                    from wizard.driver import Driver, UnixTransport
                    qmp = UnixTransport("/tmp/airtag-vm-qmp.sock")
                    mon = UnixTransport("/tmp/airtag-vm-monitor.sock")
                    Driver(qmp, mon).key("ret", post_delay=0.3)
                    emit("info", "vm", "Sent Enter to OpenCore picker")
                except Exception as e:
                    emit("warning", "vm", f"OpenCore auto-boot keypress failed: {e}")
            threading.Thread(target=worker, daemon=True).start()
        _auto_boot_opencore()

        if use_golden:
            try:
                pw_path = DATA_DIR / "vm-password"
                pw_path.write_text(VM_PASSWORD)
                pw_path.chmod(0o600)
            except Exception as e:
                emit("warning", "vm", f"Failed to write vm-password: {e}")

        return jsonify({"status": "started", "vnc_ws_port": VNC_WS_PORT})
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

    _systemctl("stop", "airtag-novnc")
    return jsonify({"status": "stopped"})


@app.route("/api/vm/start-manual", methods=["POST"])
def vm_start_manual():
    """Start the VM via VNC with NO automation.

    For the one-time manual Setup Assistant run: operator connects over
    VNC/noVNC, finishes setup by hand, then calls /api/vm/bake-golden to
    snapshot the disk. Future /api/vm/start-setup calls restore from it.
    """
    if not VM_ENABLED:
        return jsonify({"error": "VM not enabled"}), 400

    if not (VM_DIR / "mac_hdd_ng.img").exists():
        return jsonify({"error": "VM not provisioned yet"}), 400

    pid_file = Path("/tmp/airtag-vm-setup.pid")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return jsonify({"status": "already_running", "vnc_ws_port": VNC_WS_PORT})
        except (ValueError, ProcessLookupError):
            pid_file.unlink(missing_ok=True)

    emit("info", "vm", "Starting VM in MANUAL mode (no automation)")

    qemu_args = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-m", "8192",
        "-cpu", "Skylake-Client,-hle,-rtm,kvm=on,vendor=GenuineIntel,+invtsc,vmware-cpuid-freq=on,+ssse3,+sse4.2,+popcnt,+avx,+aes,+xsave,+xsaveopt,check",
        "-machine", "q35",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd,bus=xhci.0",
        "-device", "usb-tablet,bus=xhci.0",
        "-smp", "4,cores=2",
        "-global", "ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off",
        "-device", "isa-applesmc,osk=ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc",
        "-drive", f"if=pflash,format=raw,readonly=on,file={VM_DIR}/OVMF_CODE_4M.fd",
        "-drive", f"if=pflash,format=raw,file={VM_DIR}/OVMF_VARS-1920x1080.fd",
        "-smbios", "type=2",
        "-device", "ich9-ahci,id=sata",
        "-drive", f"id=OpenCoreBoot,if=none,snapshot=on,format=qcow2,file={VM_DIR}/OpenCore/OpenCore.qcow2",
        "-device", "ide-hd,bus=sata.2,drive=OpenCoreBoot",
        "-drive", f"id=MacHDD,if=none,file={VM_DIR}/mac_hdd_ng.img,format=qcow2",
        "-device", "ide-hd,bus=sata.4,drive=MacHDD",
        "-netdev", "user,id=net0,hostfwd=tcp::2222-:22",
        "-device", "vmxnet3,netdev=net0,id=net0,mac=52:54:00:c9:18:27",
        "-device", "vmware-svga",
        "-vnc", "127.0.0.1:1",
        "-monitor", "unix:/tmp/airtag-vm-monitor.sock,server,nowait",
        "-qmp", "unix:/tmp/airtag-vm-qmp.sock,server,nowait",
        "-daemonize",
        "-pidfile", "/tmp/airtag-vm-setup.pid",
    ]

    try:
        result = sp.run(qemu_args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({"error": f"Failed to start VM: {result.stderr}"}), 500
        _systemctl("start", "airtag-novnc")
        return jsonify({"status": "started", "vnc_ws_port": VNC_WS_PORT, "mode": "manual"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/bake-golden", methods=["POST"])
def vm_bake_golden():
    """Snapshot current mac_hdd_ng.img as mac_hdd_golden.img.

    Must be called while the VM is stopped (otherwise the image is
    inconsistent). Future start-setup calls restore from this image.
    """
    if not VM_ENABLED:
        return jsonify({"error": "VM not enabled"}), 400

    pid_file = Path("/tmp/airtag-vm-setup.pid")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return jsonify({"error": "VM still running — stop it first"}), 400
        except (ValueError, ProcessLookupError):
            pid_file.unlink(missing_ok=True)

    src = VM_DIR / "mac_hdd_ng.img"
    if not src.exists():
        return jsonify({"error": "mac_hdd_ng.img not found"}), 400

    dst = VM_DIR / "mac_hdd_golden.img"
    try:
        import shutil
        if dst.exists():
            backup = VM_DIR / "mac_hdd_golden.img.bak"
            emit("info", "vm", f"Existing golden image backed up to {backup.name}")
            shutil.move(str(dst), str(backup))
        emit("info", "vm", f"Baking golden image: {src.name} → {dst.name}")
        shutil.copy2(src, dst)
        size_gb = dst.stat().st_size / (1024**3)
        emit("info", "vm", f"Golden image baked ({size_gb:.1f} GB)")
        return jsonify({"status": "baked", "path": str(dst), "size_gb": round(size_gb, 2)})
    except Exception as e:
        emit("error", "vm", f"Failed to bake golden image: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/api/account/status")
def account_status():
    """Check if Apple account is configured and session is valid."""
    return jsonify(
        {
            "configured": account_exists(),
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
            save_account(acc)
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
            save_account(_pending_account)
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
    emit("info", "system", f"Account configured: {account_exists()}")
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
