"""Minimal QMP client for controlling a running QEMU VM over a Unix socket."""

from __future__ import annotations

import json
import socket
import time
from contextlib import contextmanager

from .config import MONITOR_SOCK, QMP_SOCK


_UNSHIFTED = {
    " ": "spc", "-": "minus", "=": "equal", "/": "slash", ".": "dot",
    ",": "comma", ";": "semicolon", "'": "apostrophe", "[": "bracket_left",
    "]": "bracket_right", "\\": "backslash", "`": "grave_accent",
}
_SHIFTED = {
    ":": "semicolon", '"': "apostrophe", "_": "minus", "?": "slash",
    "|": "backslash", "+": "equal", "<": "comma", ">": "dot",
    "{": "bracket_left", "}": "bracket_right", "~": "grave_accent",
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0",
}


def _ascii_to_chord(ch: str) -> list[str]:
    if ch.isupper():
        return ["shift", ch.lower()]
    if ch.isalnum():
        return [ch]
    if ch in _UNSHIFTED:
        return [_UNSHIFTED[ch]]
    if ch in _SHIFTED:
        return ["shift", _SHIFTED[ch]]
    raise ValueError(f"unmapped character for QMP send-key: {ch!r}")


class QmpError(Exception):
    pass


class QmpClient:
    """Single-request/response QMP session.

    Each instance opens one fresh connection, negotiates capabilities, and
    lets the caller issue `send_key` or raw commands before closing.
    """

    def __init__(self, path: str = QMP_SOCK, timeout: float = 3.0) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(path)
        self._buf = b""
        self._recv_json()  # server greeting
        self._send({"execute": "qmp_capabilities"})
        self._recv_json()

    def _send(self, obj: dict) -> None:
        self._sock.sendall((json.dumps(obj) + "\n").encode())

    def _recv_json(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise QmpError("QMP connection closed unexpectedly")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def send_keys(self, keys: list[str], hold_ms: int = 120, gap_s: float = 0.25) -> None:
        for k in keys:
            self._send({
                "execute": "send-key",
                "arguments": {
                    "keys": [{"type": "qcode", "data": k}],
                    "hold-time": hold_ms,
                },
            })
            self._recv_json()
            time.sleep(gap_s)

    def send_chord(self, keys: list[str], hold_ms: int = 120) -> None:
        self._send({
            "execute": "send-key",
            "arguments": {
                "keys": [{"type": "qcode", "data": k} for k in keys],
                "hold-time": hold_ms,
            },
        })
        self._recv_json()

    def type_text(self, text: str, gap_s: float = 0.04) -> None:
        for ch in text:
            chord = _ascii_to_chord(ch)
            self.send_chord(chord)
            time.sleep(gap_s)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


@contextmanager
def qmp(path: str = QMP_SOCK, timeout: float = 3.0):
    client = QmpClient(path, timeout)
    try:
        yield client
    finally:
        client.close()


def send_keys(keys: list[str], **kw) -> None:
    """One-shot convenience wrapper."""
    with qmp() as c:
        c.send_keys(keys, **kw)


def type_text(text: str, **kw) -> None:
    with qmp() as c:
        c.type_text(text, **kw)


def send_chord(keys: list[str], **kw) -> None:
    with qmp() as c:
        c.send_chord(keys, **kw)


def system_powerdown() -> None:
    """Send ACPI power-down signal (graceful shutdown)."""
    with qmp() as c:
        c._send({"execute": "system_powerdown"})
        c._recv_json()


def system_reset() -> None:
    """Send hardware reset signal via QMP (equivalent to pressing the reset button)."""
    with qmp() as c:
        c._send({"execute": "system_reset"})
        c._recv_json()


def screendump(output_path: str, monitor_path: str = MONITOR_SOCK) -> None:
    """Ask the HMP monitor to dump the framebuffer to `output_path` (PPM)."""
    hmp(f"screendump {output_path}", monitor_path=monitor_path, settle_s=1.0)


def hmp(
    command: str,
    *,
    monitor_path: str = MONITOR_SOCK,
    settle_s: float = 0.0,
    read_timeout_s: float = 30.0,
) -> str:
    """Send a human-monitor-protocol command and return the raw response text.

    HMP is what QEMU's interactive monitor speaks (`savevm`, `loadvm`,
    `info snapshots`, `screendump`, etc.).  Output format is not stable,
    so callers parse with care.

    *settle_s* is a post-write sleep for commands where the response is
    fire-and-forget (screendump, etc.).  Long-running commands like
    ``savevm`` should leave settle_s=0 and rely on the socket read to
    block until QEMU is ready for the next prompt.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(read_timeout_s)
    s.connect(monitor_path)
    try:
        # Drain the QEMU greeting + prompt before sending the command,
        # otherwise it interleaves with our response.
        _drain_until_prompt(s)
        s.sendall((command + "\n").encode())
        if settle_s > 0:
            time.sleep(settle_s)
            return ""
        # Read until the next prompt — that's how we know the command finished.
        return _read_until_prompt(s)
    finally:
        s.close()


def _drain_until_prompt(s: socket.socket, max_bytes: int = 65536) -> bytes:
    """Read whatever QEMU has buffered up to and including the `(qemu) ` prompt."""
    return _read_until_prompt(s, max_bytes=max_bytes)


def _read_until_prompt(s: socket.socket, max_bytes: int = 1 << 20) -> str:
    """Read text from *s* until we see the `(qemu) ` prompt or *max_bytes*."""
    buf = b""
    while len(buf) < max_bytes:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        # QEMU prints "(qemu) " after each command finishes.
        if buf.endswith(b"(qemu) ") or buf.rstrip().endswith(b"(qemu)"):
            break
    text = buf.decode(errors="replace")
    # Strip the trailing prompt from the visible output.
    if text.endswith("(qemu) "):
        text = text[: -len("(qemu) ")]
    return text.rstrip("\r\n")
