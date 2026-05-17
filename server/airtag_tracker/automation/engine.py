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
network hiccup).  The engine retries each state up to MAX_RETRIES times
before giving up.  Handlers signal "not done yet, retry me" by returning
the *same* state they were called with.

Abort
-----
``ctx.aborted`` is checked between every state transition.  If set, the
engine stops cleanly (without transitioning to ERROR).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from ..events import emit
from . import popup_watcher
from .context import AutomationContext
from .states import AnyState, FlowKind, InstallState, RuntimeState

MAX_RETRIES = 3
RETRY_DELAY_S = 3.0

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
            InstallState.SETUP_ASSISTANT:    setup_assistant.run,
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

        retries = 0
        while True:
            state = ctx.state
            if state in terminal:
                break
            if ctx.aborted:
                emit("info", "engine", "Abort requested — stopping engine")
                break

            emit("info", "engine", f"→ {state.value}")
            try:
                handler = _get_handler(state)
                next_state = handler(ctx)
            except (RuntimeError, TimeoutError) as e:
                msg = str(e)
                emit("error", "engine", f"State {state.value} failed: {msg}")
                error_state = (
                    InstallState.ERROR
                    if ctx.flow_kind == FlowKind.INSTALL
                    else RuntimeState.ERROR
                )
                ctx.set_state(error_state, error=msg)
                break
            except Exception as e:
                emit("error", "engine", f"Unexpected error in {state.value}: {e}")
                error_state = (
                    InstallState.ERROR
                    if ctx.flow_kind == FlowKind.INSTALL
                    else RuntimeState.ERROR
                )
                ctx.set_state(error_state, error=str(e))
                break

            if next_state == state:
                retries += 1
                if retries >= MAX_RETRIES:
                    msg = f"State {state.value} did not advance after {MAX_RETRIES} retries"
                    emit("error", "engine", msg)
                    error_state = (
                        InstallState.ERROR
                        if ctx.flow_kind == FlowKind.INSTALL
                        else RuntimeState.ERROR
                    )
                    ctx.set_state(error_state, error=msg)
                    break
                emit("warning", "engine",
                     f"State {state.value} retry {retries}/{MAX_RETRIES}")
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
