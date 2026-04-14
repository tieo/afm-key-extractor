"""Systemctl and journalctl helpers."""

from __future__ import annotations

import subprocess as sp
import threading

from .events import emit

SYSTEMCTL = "/run/current-system/sw/bin/systemctl"
SUDO = "/run/wrappers/bin/sudo"


def ctl(action: str, service: str) -> sp.CompletedProcess:
    return sp.run(
        [SUDO, SYSTEMCTL, action, service],
        capture_output=True, text=True, timeout=10,
    )


def is_active(unit: str) -> bool:
    r = sp.run(["systemctl", "is-active", unit], capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating")


def _tail(unit: str, category: str) -> None:
    try:
        proc = sp.Popen(
            ["journalctl", "-u", unit, "-f", "-n", "0", "--no-hostname", "-o", "cat"],
            stdout=sp.PIPE, stderr=sp.PIPE, text=True,
        )
        emit("info", category, f"Streaming logs for {unit}")
        for line in proc.stdout:
            line = line.strip()
            if line:
                emit("info", category, line)
            if not is_active(unit):
                break
        proc.terminate()
        rc = sp.run(
            ["systemctl", "show", unit, "-p", "ExecMainStatus", "--value"],
            capture_output=True, text=True,
        )
        code = rc.stdout.strip()
        if code == "0":
            emit("info", category, f"{unit} completed successfully")
        else:
            emit("error", category, f"{unit} exited with code {code}")
    except Exception as e:
        emit("error", category, f"Journal tail error: {e}")


def tail_journal_async(unit: str, category: str) -> None:
    threading.Thread(target=_tail, args=(unit, category), daemon=True).start()
