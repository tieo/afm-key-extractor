"""Debug/iteration endpoints for the snapshot+replay harness.

Prefix: /api/debug

Snapshot ops are direct passthroughs to ``vm.snapshot.*``.  The replay
endpoint takes a state name, restores the matching snapshot, builds a
minimal AutomationContext, and invokes that one handler — letting an
operator iterate on a single state in seconds instead of running a full
install flow each time.

All endpoints require the VM to be running.  Snapshots are stored inside
the qcow2 disk files, so they survive QEMU stop/start but not disk wipes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ... import vm
from ...automation import engine
from ...automation.context import AutomationContext
from ...automation.states import (
    INSTALL_STAGE_LABELS,
    RUNTIME_STAGE_LABELS,
    FlowKind,
    InstallState,
    RuntimeState,
)
from ...vm_password import ensure as ensure_vm_password
from .. import sse

router = APIRouter(prefix="/api/debug", tags=["debug"])


# ---------------------------------------------------------------------------
# Snapshot CRUD
# ---------------------------------------------------------------------------

class _Label(BaseModel):
    label: str


@router.post("/snapshot")
def save_snapshot(body: _Label) -> dict:
    try:
        return vm.snapshot.save(body.label)
    except vm.VmError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/restore")
def restore_snapshot(body: _Label) -> dict:
    try:
        return vm.snapshot.load(body.label)
    except vm.VmError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/snapshots")
def list_snapshots() -> list[dict]:
    try:
        return vm.snapshot.list_all()
    except vm.VmError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/snapshot/{label}")
def delete_snapshot(label: str) -> dict:
    try:
        return vm.snapshot.delete(label)
    except vm.VmError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Replay a single handler
# ---------------------------------------------------------------------------

class _ReplayBody(BaseModel):
    state: str
    restore_label: str | None = None     # snapshot to load before invoking
    apple_email: str = ""
    apple_password: str = ""


def _resolve_state(name: str):
    """Accept either an InstallState or RuntimeState value string."""
    try:
        return InstallState(name)
    except ValueError:
        pass
    try:
        return RuntimeState(name)
    except ValueError:
        pass
    return None


@router.post("/run-handler")
def run_handler(body: _ReplayBody) -> dict:
    """Restore *restore_label* (optional) and invoke the *state* handler once.

    Returns ``{state, next_state, label, error}``.  Does NOT start the engine
    main loop — runs the handler synchronously and returns its result so an
    operator can iterate on a single screen without burning a full install.
    """
    state = _resolve_state(body.state)
    if state is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown state {body.state!r} (not an InstallState or RuntimeState)",
        )
    if engine._engine.is_running:
        raise HTTPException(
            status_code=409,
            detail="Engine is running — abort or wait before invoking a debug handler",
        )

    # Optional pre-restore.
    if body.restore_label:
        try:
            vm.snapshot.load(body.restore_label)
        except vm.VmError as e:
            raise HTTPException(status_code=400, detail=str(e))

    flow = FlowKind.INSTALL if isinstance(state, InstallState) else FlowKind.RUNTIME
    ctx = AutomationContext(
        flow_kind=flow,
        vm_password=ensure_vm_password(),
        apple_email=body.apple_email,
        apple_password=body.apple_password,
        initial_state=state,
    )
    ctx._broadcast = sse.broadcast

    handler = engine._get_handler(state)
    try:
        next_state = handler(ctx)
    except Exception as e:
        return {
            "state": state.value,
            "next_state": None,
            "label": _label_for(flow, state.value),
            "error": str(e),
        }
    return {
        "state": state.value,
        "next_state": next_state.value if next_state else None,
        "label": _label_for(flow, state.value),
        "error": None,
    }


def _label_for(flow: FlowKind, state_val: str) -> str:
    table = INSTALL_STAGE_LABELS if flow == FlowKind.INSTALL else RUNTIME_STAGE_LABELS
    cls = InstallState if flow == FlowKind.INSTALL else RuntimeState
    try:
        return table[cls(state_val)]
    except (KeyError, ValueError):
        return state_val
