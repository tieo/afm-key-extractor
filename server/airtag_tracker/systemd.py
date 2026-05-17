"""Thin wrapper around systemctl for managing service units."""

from __future__ import annotations

import subprocess

from .events import emit


def ctl(action: str, unit: str) -> None:
    """Run `systemctl <action> <unit>`, logging but not raising on failure."""
    result = subprocess.run(
        ["systemctl", action, unit],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        emit(
            "warning",
            "systemd",
            f"systemctl {action} {unit} exited {result.returncode}: "
            f"{result.stderr.strip()[:200]}",
        )
