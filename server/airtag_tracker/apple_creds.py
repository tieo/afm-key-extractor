"""In-memory Apple ID credential cache.

Kept only in RAM for the duration of a sign-in flow into the VM. Never
persisted. Cleared on success/failure or server restart. The web login
populates this right after the user submits the password; the VM
sign-in orchestrator consumes it, then clears it.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_email: str | None = None
_password: str | None = None


def set_(email: str, password: str) -> None:
    global _email, _password
    with _lock:
        _email = email
        _password = password


def get() -> tuple[str, str] | None:
    with _lock:
        if _email and _password:
            return _email, _password
        return None


def clear() -> None:
    global _email, _password
    with _lock:
        _email = None
        _password = None
