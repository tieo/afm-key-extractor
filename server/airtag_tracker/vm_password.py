"""VM account password — persisted in DATA_DIR, generated on first use.

The same password is used in three places: (1) typed into the macOS
Setup Assistant when creating the local account, (2) typed into the
login window by the auto-login routine on every subsequent boot, and
(3) marker for "VM setup is complete" (the file's existence).
"""

from __future__ import annotations

import secrets

from .config import VM_PASSWORD_PATH


def get() -> str | None:
    """Return the stored password, or None if the VM hasn't been set up yet."""
    if not VM_PASSWORD_PATH.exists():
        return None
    return VM_PASSWORD_PATH.read_text().rstrip("\n")


def ensure() -> str:
    """Return existing password, or generate and persist a new random one."""
    existing = get()
    if existing:
        return existing
    pw = secrets.token_hex(6)   # 12-char hex [0-9a-f]: no Shift needed; 32-char fails verify
    VM_PASSWORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    VM_PASSWORD_PATH.write_text(pw)
    VM_PASSWORD_PATH.chmod(0o600)
    return pw
