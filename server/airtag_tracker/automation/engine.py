"""State machine engine.

``StateMachine.run(ctx)`` executes the chosen flow in a background
thread.  Each iteration calls the handler for the current state, which
returns the next state to transition to.  Transitions are broadcast as
SSE events via ``ctx._broadcast``.

Handler contract
----------------
Every handler module exposes::

    def run(ctx: AutomationContext) -> AnyState

It may raise ``RuntimeError`` (unrecoverable, moves to ERROR) or
``TimeoutError`` (also unrecoverable, moves to ERROR).  The engine
catches both and logs the message.

Retry
-----
Transient failures are expected (screendump times out, OCR misses,
network hiccup).  The engine retries each state up to its retry budget
(`STATE_RETRY_BUDGET[state]`, defaulting to `DEFAULT_RETRY_BUDGET`) before
giving up.  Handlers signal "not done yet, retry me" by returning the
*same* state they were called with.

Abort
-----
``ctx.aborted`` is checked between every state transition.  If set, the
engine stops cleanly (without transitioning to ERROR).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import os

from ..events import emit
from . import failure_capture, popup_watcher
from .context import AutomationContext
from .states import (
    DEFAULT_RETRY_BUDGET,
    STATE_RETRY_BUDGET,
    AnyState,
    FlowKind,
    InstallState,
    RuntimeState,
)

RETRY_DELAY_S = 3.0


def _retry_budget(state: AnyState) -> int:
    """Per-state retry quota (defaults to DEFAULT_RETRY_BUDGET)."""
    return STATE_RETRY_BUDGET.get(state, DEFAULT_RETRY_BUDGET)


def _auto_snapshot_states() -> set[str]:
    """Parse ``AIRTAG_AUTO_SNAPSHOT_STATES`` (comma-separated state values).

    When a state listed here is *entered*, the engine snapshots the VM under
    that state's name.  Use to checkpoint right before flaky handlers (e.g.
    ``sa_create_account``) so a retry restores instead of replaying the
    install from scratch.
    """
    raw = os.environ.get("AIRTAG_AUTO_SNAPSHOT_STATES", "").strip()
    if not raw:
        return set()
    return {s.strip() for s in raw.split(",") if s.strip()}


def _try_auto_snapshot(state_value: str, flow_kind: "FlowKind") -> None:
    """Best-effort auto-checkpoint — never aborts the flow on failure."""
    from .. import vm
    try:
        vm.snapshot.save(state_value)
    except Exception as e:
        msg = str(e)
        # During install, InstallMedia is a raw disk that can't store snapshot
        # data — this is expected and not actionable. Log at info, not warning.
        if flow_kind == FlowKind.INSTALL and "does not support snapshots" in msg:
            emit("info", "engine",
                 f"Auto-snapshot skipped at {state_value} (InstallMedia is raw — expected during install)")
        else:
            emit("warning", "engine", f"Auto-snapshot at {state_value} failed: {e}")


def _safe_capture(state_value: str, error: str) -> None:
    """Best-effort post-mortem (screen + log + snapshot).  Never raises."""
    try:
        failure_capture.capture(state_value, error)
    except Exception as e:
        emit("warning", "engine", f"failure_capture raised: {e}")

# States that are terminal — engine exits when reached.
_INSTALL_TERMINAL = {InstallState.DONE, InstallState.ERROR}
_RUNTIME_TERMINAL = {RuntimeState.DONE, RuntimeState.ERROR}


def _get_handler(state: AnyState) -> Callable[[AutomationContext], AnyState]:
    """Import and return the handler function for *state*."""
    if isinstance(state, InstallState):
        from .install import (
            finalize,
            format_disk,
            opencore,
            reinstall,
            setup_assistant,
        )
        mapping: dict[InstallState, Callable] = {
            InstallState.IDLE:               _noop_advance(InstallState.BOOTING_PICKER),
            InstallState.BOOTING_PICKER:     opencore.wait_for_picker,
            InstallState.PICKER_SELECTING:   opencore.select_installer,
            InstallState.WAITING_RECOVERY:   opencore.wait_for_recovery,
            InstallState.FORMAT_DISK:        format_disk.run,
            InstallState.WAITING_FORMAT_DONE: format_disk.wait_done,
            InstallState.REINSTALL_CLICKING: reinstall.click_through,
            InstallState.WAITING_INSTALL:    reinstall.wait_complete,
            InstallState.BOOTING_INSTALLED:  opencore.select_installed,
            InstallState.SA_COUNTRY:         setup_assistant.screen_country,
            InstallState.SA_LANGUAGES:       setup_assistant.screen_languages,
            InstallState.SA_ACCESSIBILITY:   setup_assistant.screen_accessibility,
            InstallState.SA_DATA_PRIVACY:    setup_assistant.screen_data_privacy,
            InstallState.SA_MIGRATION:       setup_assistant.screen_migration,
            InstallState.SA_APPLE_ID:        setup_assistant.screen_apple_id,
            InstallState.SA_TERMS:           setup_assistant.screen_terms,
            InstallState.SA_CREATE_ACCOUNT:  setup_assistant.screen_create_account,
            InstallState.SA_APPLE_ID_2:      setup_assistant.screen_apple_id_2,
            InstallState.SA_TERMS_2:         setup_assistant.screen_terms_2,
            InstallState.SA_LOCATION:        setup_assistant.screen_location,
            InstallState.SA_TIMEZONE:        setup_assistant.screen_timezone,
            InstallState.SA_ANALYTICS:       setup_assistant.screen_analytics,
            InstallState.SA_SCREEN_TIME:     setup_assistant.screen_screen_time,
            InstallState.SA_APPEARANCE:      setup_assistant.screen_appearance,
            InstallState.DISMISS_FIRST_BOOT: finalize.dismiss_first_boot,
            InstallState.SHUTTING_DOWN:      finalize.shutdown,
            InstallState.BAKING_GOLDEN:      finalize.bake_golden,
            InstallState.DONE:               _noop,
            InstallState.ERROR:              _noop,
        }
        return mapping[state]  # type: ignore[index]

    from .runtime import (
        apple_signin,
        boot,
        extract,
        login,
        post_signin,
    )
    mapping_rt: dict[RuntimeState, Callable] = {
        RuntimeState.IDLE:                    _noop_advance(RuntimeState.RESTORING_GOLDEN),
        RuntimeState.RESTORING_GOLDEN:        boot.restore_golden,
        RuntimeState.BOOTING:                 boot.start_vm,
        RuntimeState.PICKER_SELECTING:        boot.select_macos,
        RuntimeState.WAITING_LOGIN_SCREEN:    login.wait_for_login_screen,
        RuntimeState.LOGGING_IN:              login.log_in,
        RuntimeState.WAITING_DESKTOP:         login.wait_for_desktop,
        RuntimeState.DISABLING_SLEEP:         login.disable_sleep,
        RuntimeState.OPENING_APPLE_ID:        apple_signin.open_apple_id,
        RuntimeState.TYPING_CREDENTIALS:      apple_signin.type_credentials,
        RuntimeState.WAITING_2FA_OR_SIGNED_IN: apple_signin.wait_2fa_or_signed_in,
        RuntimeState.AWAITING_2FA:            apple_signin.await_2fa_input,
        RuntimeState.TYPING_2FA:              apple_signin.type_2fa,
        RuntimeState.WAITING_SIGNED_IN:       apple_signin.wait_signed_in,
        RuntimeState.DISMISSING_POST_SIGNIN:  post_signin.dismiss_prompts,
        RuntimeState.RESOLVING_APPLE_ID_UPDATE: post_signin.resolve_update,
        RuntimeState.ENABLING_FIND_MY:        post_signin.enable_find_my,
        RuntimeState.WAITING_ICLOUD_SYNC:     extract.wait_icloud_sync,
        RuntimeState.EXTRACTING_KEYS:         extract.run,
        RuntimeState.SHUTTING_DOWN:           extract.shutdown,
        RuntimeState.DONE:                    _noop,
        RuntimeState.ERROR:                   _noop,
    }
    return mapping_rt[state]  # type: ignore[index]


def _noop(ctx: AutomationContext) -> AnyState:
    return ctx.state


def _noop_advance(next_state: AnyState) -> Callable[[AutomationContext], AnyState]:
    def _handler(ctx: AutomationContext) -> AnyState:
        return next_state
    return _handler


class StateMachine:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run(self, ctx: AutomationContext, broadcast: Callable[[dict], None]) -> None:
        if self.is_running:
            raise RuntimeError("engine already running")
        ctx._broadcast = broadcast
        self._thread = threading.Thread(
            target=self._loop,
            args=(ctx,),
            daemon=True,
            name=f"engine-{ctx.flow_kind.value}",
        )
        self._thread.start()

    def _loop(self, ctx: AutomationContext) -> None:
        terminal = (
            _INSTALL_TERMINAL
            if ctx.flow_kind == FlowKind.INSTALL
            else _RUNTIME_TERMINAL
        )

        # Start popup watcher for all runtime flows and during setup assistant.
        if ctx.flow_kind == FlowKind.RUNTIME:
            popup_watcher.start(ctx)

        snapshot_states = _auto_snapshot_states()
        snapshotted: set[str] = set()
        # Once-per-flow GC of orphan `fail_*` snapshots inside the qcow2 disks.
        # The disk image only accepts delvm while the VM is running, so we
        # poll the flag until the first handler brings the VM up, then run
        # cleanup exactly once.
        gc_pending = True

        retries = 0
        while True:
            state = ctx.state
            if state in terminal:
                break
            if ctx.aborted:
                emit("info", "engine", "Abort requested — stopping engine")
                break

            if gc_pending:
                try:
                    from .. import vm
                    if vm.is_running():
                        failure_capture.gc_orphan_snapshots()
                        gc_pending = False
                except Exception:
                    pass

            # Optional auto-snapshot: once per state per run, take a snapshot
            # at the entry to debug-flagged states so retries can restore
            # rather than replay the whole install.
            if state.value in snapshot_states and state.value not in snapshotted:
                _try_auto_snapshot(state.value, ctx.flow_kind)
                snapshotted.add(state.value)

            emit("info", "engine", f"→ {state.value}")
            try:
                handler = _get_handler(state)
                next_state = handler(ctx)
            except (RuntimeError, TimeoutError) as e:
                msg = str(e)
                emit("error", "engine", f"State {state.value} failed: {msg}")
                _safe_capture(state.value, msg)
                error_state = (
                    InstallState.ERROR
                    if ctx.flow_kind == FlowKind.INSTALL
                    else RuntimeState.ERROR
                )
                ctx.set_state(error_state, error=msg)
                break
            except Exception as e:
                emit("error", "engine", f"Unexpected error in {state.value}: {e}")
                _safe_capture(state.value, str(e))
                error_state = (
                    InstallState.ERROR
                    if ctx.flow_kind == FlowKind.INSTALL
                    else RuntimeState.ERROR
                )
                ctx.set_state(error_state, error=str(e))
                break

            if next_state == state:
                retries += 1
                budget = _retry_budget(state)
                if retries >= budget:
                    msg = f"State {state.value} did not advance after {budget} retries"
                    emit("error", "engine", msg)
                    _safe_capture(state.value, msg)
                    error_state = (
                        InstallState.ERROR
                        if ctx.flow_kind == FlowKind.INSTALL
                        else RuntimeState.ERROR
                    )
                    ctx.set_state(error_state, error=msg)
                    break
                emit("warning", "engine",
                     f"State {state.value} retry {retries}/{budget}")
                time.sleep(RETRY_DELAY_S)
            else:
                retries = 0
                ctx.set_state(next_state)

        popup_watcher.stop()
        emit("info", "engine", f"Engine finished. Final state: {ctx.state.value}")


# Module-level singleton — one engine per process is the right constraint.
_engine = StateMachine()
_ctx: AutomationContext | None = None
_ctx_lock = threading.Lock()


def get_context() -> AutomationContext | None:
    with _ctx_lock:
        return _ctx


def start_flow(ctx: AutomationContext, broadcast: Callable[[dict], None]) -> None:
    global _ctx
    if _engine.is_running:
        raise RuntimeError("An automation flow is already running")
    with _ctx_lock:
        _ctx = ctx
    _engine.run(ctx, broadcast)


def abort() -> None:
    with _ctx_lock:
        c = _ctx
    if c:
        c.request_abort()
