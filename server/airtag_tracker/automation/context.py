"""Shared mutable state for a single automation run.

One instance lives for the duration of a flow (install or runtime).
The engine and all handler modules read/write it through the provided
methods — never by touching private attributes directly.

Thread safety
-------------
``_lock`` protects all mutable fields.  The 2FA pause is implemented as
a ``threading.Event`` so the engine thread can block cheaply and be
unblocked from the API thread without polling.

QMP serialisation
-----------------
``qmp_lock`` is a reentrant lock that every thread must hold while
sending a *sequence* of QMP commands (e.g., move + click + release).
Single atomic commands (screendump, single keypress) do not need it.
This prevents the popup watcher from injecting a click between a mouse-
down and mouse-up issued by the main flow thread.
"""

from __future__ import annotations

import threading

from .states import AnyState, FlowKind, InstallState, RuntimeState


class AutomationContext:
    def __init__(
        self,
        flow_kind: FlowKind,
        vm_password: str,
        apple_email: str = "",
        apple_password: str = "",
        restore_golden: bool = True,
        icloud_sync_timeout_s: int = 1800,
        initial_state: AnyState | None = None,
    ) -> None:
        self.flow_kind = flow_kind
        self.vm_password = vm_password
        self.apple_email = apple_email
        self.apple_password = apple_password
        self.restore_golden = restore_golden
        self.icloud_sync_timeout_s = icloud_sync_timeout_s

        self._lock = threading.Lock()
        self.qmp_lock = threading.RLock()

        if initial_state is not None:
            initial: AnyState = initial_state
        else:
            initial = (
                InstallState.IDLE if flow_kind == FlowKind.INSTALL else RuntimeState.IDLE
            )
        self._state: AnyState = initial
        self._error: str | None = None

        # 2FA pause/resume
        self._2fa_code: str | None = None
        self._2fa_event = threading.Event()
        self._sms_event = threading.Event()
        self._sms_phone: str | None = None

        # Abort flag — checked by engine between state transitions.
        self._abort = False

        # SSE broadcast hook — set by the engine after construction.
        self._broadcast: "callable[[dict], None] | None" = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> AnyState:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def set_state(self, new: AnyState, error: str | None = None) -> None:
        with self._lock:
            self._state = new
            self._error = error
        self._emit_state_event(new, error)

    def _emit_state_event(self, state: AnyState, error: str | None) -> None:
        if self._broadcast is None:
            return
        from datetime import UTC, datetime
        payload: dict = {
            "type": "state",
            "flow": self.flow_kind.value,
            "state": state.value,
            "ts": datetime.now(UTC).isoformat(),
        }
        if error:
            payload["error"] = error
        try:
            self._broadcast(payload)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Abort
    # ------------------------------------------------------------------

    def request_abort(self) -> None:
        with self._lock:
            self._abort = True

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._abort

    # ------------------------------------------------------------------
    # 2FA pause / resume
    # ------------------------------------------------------------------

    def wait_for_2fa(self, timeout_s: float = 600.0) -> str:
        """Block the engine thread until a 2FA code is delivered or timeout."""
        self._2fa_event.wait(timeout=timeout_s)
        with self._lock:
            code = self._2fa_code
        if not code:
            raise TimeoutError("2FA code not supplied within timeout")
        return code

    def deliver_2fa(self, code: str) -> None:
        """Called from the API thread to resume the blocked engine."""
        with self._lock:
            self._2fa_code = code
        self._2fa_event.set()

    def request_sms(self) -> None:
        """Signal the engine that the user wants an SMS code instead."""
        self._sms_event.set()

    def sms_was_requested(self) -> bool:
        if self._sms_event.is_set():
            self._sms_event.clear()
            return True
        return False

    @property
    def sms_phone(self) -> str | None:
        with self._lock:
            return self._sms_phone

    def set_sms_phone(self, phone: str | None) -> None:
        with self._lock:
            self._sms_phone = phone
        if self._broadcast and phone:
            from datetime import UTC, datetime
            try:
                self._broadcast({
                    "type": "sms_phone",
                    "phone": phone,
                    "ts": datetime.now(UTC).isoformat(),
                })
            except Exception:
                pass

    def clear_2fa(self) -> None:
        with self._lock:
            self._2fa_code = None
        self._2fa_event.clear()
        self._sms_event.clear()
