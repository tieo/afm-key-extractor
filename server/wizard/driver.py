"""Thin QMP + monitor socket driver.

Three primitives: ``key``, ``click``, ``screenshot``. Everything else is
built on these. No screen understanding lives here — that's the step
handlers' job.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Protocol


class Transport(Protocol):
    """Byte-level socket transport. Abstracted so tests can inject fakes."""

    def send(self, data: bytes) -> None: ...
    def recv(self, n: int) -> bytes: ...
    def close(self) -> None: ...


class UnixTransport:
    def __init__(self, path: str, timeout: float = 5.0) -> None:
        self._s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._s.settimeout(timeout)
        self._s.connect(path)

    def send(self, data: bytes) -> None:
        self._s.sendall(data)

    def recv(self, n: int) -> bytes:
        return self._s.recv(n)

    def close(self) -> None:
        self._s.close()


FB_WIDTH = 1280
FB_HEIGHT = 800
ABS_MAX = 32767


class Driver:
    """Drives a running QEMU via QMP (input) and HMP (screendump)."""

    def __init__(
        self,
        qmp: Transport,
        monitor: Transport,
        sleep: "callable[[float], None]" = time.sleep,
    ) -> None:
        self._qmp = qmp
        self._mon = monitor
        self._sleep = sleep
        self._qmp_ready = False

    # --- QMP ---

    def _qmp_cmd(self, cmd: dict) -> dict:
        if not self._qmp_ready:
            # Discard greeting, negotiate capabilities.
            self._qmp.recv(4096)
            self._qmp.send(b'{"execute":"qmp_capabilities"}\n')
            self._qmp.recv(4096)
            self._qmp_ready = True
        self._qmp.send((json.dumps(cmd) + "\n").encode())
        raw = self._qmp.recv(4096).decode(errors="replace")
        return json.loads(raw.splitlines()[-1]) if raw.strip() else {}

    def key(self, qcode: str, post_delay: float = 0.2) -> None:
        """Send a single key (QEMU qcode: ``ret``, ``right``, ``a``, …)."""
        self._qmp_cmd(
            {
                "execute": "send-key",
                "arguments": {"keys": [{"type": "qcode", "data": qcode}]},
            }
        )
        self._sleep(post_delay)

    def click(self, x: int, y: int, post_delay: float = 0.3) -> None:
        """Click at pixel (x, y) in the ``FB_WIDTH x FB_HEIGHT`` framebuffer."""
        qx = int(x / FB_WIDTH * ABS_MAX)
        qy = int(y / FB_HEIGHT * ABS_MAX)
        self._qmp_cmd(
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
        self._sleep(0.05)
        self._qmp_cmd(
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [{"type": "btn", "data": {"down": True, "button": "left"}}]
                },
            }
        )
        self._sleep(0.05)
        self._qmp_cmd(
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [{"type": "btn", "data": {"down": False, "button": "left"}}]
                },
            }
        )
        self._sleep(post_delay)

    # --- HMP monitor ---

    def screenshot(self, path: str | Path) -> bytes:
        """Write a PPM screendump to ``path`` and return the raw bytes."""
        path = Path(path)
        path.unlink(missing_ok=True)
        self._mon.send(f"screendump {path}\n".encode())
        # Drain monitor response (best-effort; HMP echoes prompt).
        self._sleep(0.5)
        try:
            self._mon.recv(4096)
        except (TimeoutError, OSError):
            pass
        return path.read_bytes()
