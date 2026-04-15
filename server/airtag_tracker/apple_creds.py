"""Apple ID credential cache.

RAM-first; optionally persisted at rest (Fernet key derived from
/etc/machine-id, same scheme as account_storage). Persistence lets
the VM sign-in flow fire automatically on server restart — otherwise
every deploy would require the user to re-enter their password.

The VM sign-in worker still clears both RAM and disk once Apple has
confirmed sign-in (`signed_in` marker persisted separately), so the
password doesn't live longer than needed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from pathlib import Path

from cryptography.fernet import Fernet

from .config import DATA_DIR

_CREDS_PATH = DATA_DIR / "apple-creds.enc"
_SALT = b"airtag-tracker-apple-creds-v1"
_ITERATIONS = 200_000

_lock = threading.Lock()
_email: str | None = None
_password: str | None = None


def _fernet() -> Fernet:
    mid = Path("/etc/machine-id").read_text().strip().encode()
    key = hashlib.pbkdf2_hmac("sha256", mid, _SALT, _ITERATIONS, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def _persist(email: str, password: str) -> None:
    try:
        _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"email": email, "password": password}).encode()
        _CREDS_PATH.write_bytes(_fernet().encrypt(payload))
        _CREDS_PATH.chmod(0o600)
    except Exception:
        pass


def _load_from_disk() -> tuple[str, str] | None:
    if not _CREDS_PATH.exists():
        return None
    try:
        data = json.loads(_fernet().decrypt(_CREDS_PATH.read_bytes()))
        return data.get("email"), data.get("password")
    except Exception:
        return None


def set_(email: str, password: str) -> None:
    global _email, _password
    with _lock:
        _email = email
        _password = password
    _persist(email, password)


def get() -> tuple[str, str] | None:
    global _email, _password
    with _lock:
        if _email and _password:
            return _email, _password
    disk = _load_from_disk()
    if disk and disk[0] and disk[1]:
        with _lock:
            _email, _password = disk
            return _email, _password
    return None


def clear() -> None:
    global _email, _password
    with _lock:
        _email = None
        _password = None
    try:
        _CREDS_PATH.unlink()
    except FileNotFoundError:
        pass
