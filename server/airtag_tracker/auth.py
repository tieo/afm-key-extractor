"""Apple ID login flow — pending-account state machine for 2FA."""

from __future__ import annotations

import threading

from findmy import AppleAccount, LocalAnisetteProvider
from findmy.reports import LoginState, SyncSmsSecondFactor

from . import account_storage
from .config import ANISETTE_PATH
from .events import emit

_lock = threading.Lock()
_pending_account: AppleAccount | None = None
_pending_methods: list | None = None


class LoginError(Exception):
    pass


def _method_descriptor(m) -> dict:
    if isinstance(m, SyncSmsSecondFactor):
        return {"type": "sms", "phone": m.phone_number, "id": m.phone_number_id}
    return {"type": "trusted_device"}


def begin(email: str, password: str) -> dict:
    """Start login. Returns either {'status': 'logged_in'} or
    {'status': '2fa_required', 'methods': [...]}."""
    global _pending_account, _pending_methods

    if not email or not password:
        raise LoginError("email and password required")

    emit("info", "account", f"Logging in as {email}")
    ani = LocalAnisetteProvider(libs_path=str(ANISETTE_PATH))
    acc = AppleAccount(ani)
    state = acc.login(email, password)

    with _lock:
        if state == LoginState.REQUIRE_2FA:
            methods = acc.get_2fa_methods()
            _pending_account = acc
            _pending_methods = methods
            if methods:
                methods[0].request()
            emit("info", "account", f"2FA required ({len(methods)} method(s) available)")
            return {"status": "2fa_required",
                    "methods": [_method_descriptor(m) for m in methods]}

        if state == LoginState.LOGGED_IN:
            account_storage.save(acc)
            _pending_account = None
            _pending_methods = None
            emit("info", "account", "Logged in successfully (no 2FA needed)")
            return {"status": "logged_in"}

        emit("error", "account", f"Unexpected login state: {state}")
        raise LoginError(f"Unexpected login state: {state}")


def submit_2fa(code: str, method_index: int) -> dict:
    global _pending_account, _pending_methods
    with _lock:
        if not _pending_account or not _pending_methods:
            raise LoginError("No pending login. Call begin() first.")
        if not code:
            raise LoginError("code required")
        method = _pending_methods[method_index]
        state = method.submit(code)
        if state == LoginState.LOGGED_IN:
            account_storage.save(_pending_account)
            _pending_account = None
            _pending_methods = None
            emit("info", "account", "2FA verified, logged in successfully")
            return {"status": "logged_in"}
        emit("warning", "account", f"2FA rejected (state: {state})")
        raise LoginError(f"2FA failed, state: {state}")


def request_2fa(method_index: int) -> None:
    with _lock:
        if not _pending_methods:
            raise LoginError("No pending login.")
        _pending_methods[method_index].request()


def clear_pending() -> None:
    global _pending_account, _pending_methods
    with _lock:
        _pending_account = None
        _pending_methods = None
