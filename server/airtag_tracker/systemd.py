"""Service management — systemctl wrapper with websockify fallback for dev."""

from __future__ import annotations

import shutil
import subprocess
import threading

from .config import VNC_WS_PORT
from .events import emit

# noVNC web root: populated by _find_novnc_web() below.
_NOVNC_WEB: str | None = None
_novnc_proc: subprocess.Popen | None = None
_novnc_lock = threading.Lock()


def _find_novnc_web() -> str | None:
    for candidate in (
        "/run/current-system/sw/share/webapps/novnc",
        "/usr/share/novnc",
        "/usr/share/webapps/novnc",
    ):
        if shutil.os.path.isdir(candidate):
            return candidate
    # Try the nix store path that websockify puts novnc under.
    try:
        r = subprocess.run(
            ["nix-shell", "-p", "novnc", "--run", "echo $novnc"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            candidate = r.stdout.strip() + "/share/webapps/novnc"
            if shutil.os.path.isdir(candidate):
                return candidate
    except Exception:
        pass
    return None


def _start_novnc_fallback() -> None:
    """Start websockify directly when systemd unit is not available."""
    global _novnc_proc
    with _novnc_lock:
        if _novnc_proc is not None and _novnc_proc.poll() is None:
            return  # already running

    web = _find_novnc_web()
    cmd = ["websockify", f"127.0.0.1:{VNC_WS_PORT}", "127.0.0.1:5901"]
    if web:
        cmd = ["websockify", "--web", web, f"127.0.0.1:{VNC_WS_PORT}", "127.0.0.1:5901"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with _novnc_lock:
            _novnc_proc = proc
        emit("info", "systemd", f"noVNC websockify started (PID {proc.pid}, port {VNC_WS_PORT})")
    except FileNotFoundError:
        emit("warning", "systemd",
             "websockify not found — noVNC will not be available. "
             "Install: nix-shell -p python3Packages.websockify")


def _stop_novnc_fallback() -> None:
    global _novnc_proc
    with _novnc_lock:
        proc = _novnc_proc
        _novnc_proc = None
    if proc and proc.poll() is None:
        proc.terminate()
        emit("info", "systemd", "noVNC websockify stopped")


def ctl(action: str, unit: str) -> None:
    """Run `systemctl <action> <unit>`.

    Falls back to direct websockify management when systemctl is
    unavailable or the unit doesn't exist (dev/Docker environments).
    """
    result = subprocess.run(
        ["systemctl", action, unit],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return

    # systemctl failed — try the direct fallback for noVNC.
    if unit == "airtag-novnc":
        if action == "start":
            _start_novnc_fallback()
        elif action == "stop":
            _stop_novnc_fallback()
        return

    emit(
        "warning",
        "systemd",
        f"systemctl {action} {unit} exited {result.returncode}: "
        f"{result.stderr.strip()[:200]}",
    )
