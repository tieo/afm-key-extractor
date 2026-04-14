"""Apple account at-rest encryption.

Fernet key derived via PBKDF2-HMAC-SHA256 from /etc/machine-id with a
static app-specific salt. Migrates any legacy plaintext account.json to
the encrypted file on first load.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.fernet import Fernet
from findmy import AppleAccount

from .config import ACCOUNT_ENC_PATH, ACCOUNT_PATH
from .events import emit

_SALT = b"airtag-tracker-account-v1"
_ITERATIONS = 200_000


def _fernet() -> Fernet:
    mid = Path("/etc/machine-id").read_text().strip().encode()
    key = hashlib.pbkdf2_hmac("sha256", mid, _SALT, _ITERATIONS, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def exists() -> bool:
    return ACCOUNT_ENC_PATH.exists() or ACCOUNT_PATH.exists()


def save(acc: AppleAccount) -> None:
    payload = json.dumps(acc.to_json()).encode()
    ACCOUNT_ENC_PATH.write_bytes(_fernet().encrypt(payload))
    ACCOUNT_ENC_PATH.chmod(0o600)


def load(anisette) -> AppleAccount | None:
    if ACCOUNT_ENC_PATH.exists():
        data = json.loads(_fernet().decrypt(ACCOUNT_ENC_PATH.read_bytes()))
        return AppleAccount.from_json(data, anisette=anisette)
    if ACCOUNT_PATH.exists():
        acc = AppleAccount.from_json(str(ACCOUNT_PATH), anisette=anisette)
        save(acc)
        ACCOUNT_PATH.unlink()
        emit("info", "account", "Migrated plaintext account.json → encrypted account.enc")
        return acc
    return None
