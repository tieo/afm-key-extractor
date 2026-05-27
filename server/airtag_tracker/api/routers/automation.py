"""Automation flow control endpoints.

Prefix: /api/automation
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...automation import engine
from ...automation.context import AutomationContext
from ...automation.states import (
    INSTALL_STAGE_LABELS,
    RUNTIME_STAGE_LABELS,
    FlowKind,
    InstallState,
    RuntimeState,
)
from ...config import APPLE_EMAIL, APPLE_PASSWORD, IPHONE_PASSCODE, VM_DIR
from ...macos_adapter import get_active_adapter
from ...vm_password import ensure as ensure_vm_password
from .. import sse

router = APIRouter(prefix="/api/automation", tags=["automation"])


def _resolve_credentials(body_email: str, body_password: str) -> tuple[str, str]:
    """Return (email, password), preferring body values over saved env-var defaults.

    Raises HTTP 400 if neither source provides both fields.
    """
    email = body_email.strip() or APPLE_EMAIL
    password = body_password or APPLE_PASSWORD
    if not email or not password:
        raise HTTPException(
            status_code=400,
            detail=(
                "Apple ID email and password are required. "
                "Provide them in the request body or set "
                "AIRTAG_APPLE_EMAIL / AIRTAG_APPLE_PASSWORD in the server environment."
            ),
        )
    return email, password


def _label_for(flow: str | None, state_val: str) -> str:
    if flow == FlowKind.INSTALL.value:
        try:
            return INSTALL_STAGE_LABELS[InstallState(state_val)]
        except (KeyError, ValueError):
            pass
    if flow == FlowKind.RUNTIME.value:
        try:
            return RUNTIME_STAGE_LABELS[RuntimeState(state_val)]
        except (KeyError, ValueError):
            pass
    return state_val


@router.get("/status")
def get_status() -> dict:
    ctx = engine.get_context()
    running = engine._engine.is_running
    if ctx is None:
        return {
            "flow": None,
            "state": "idle",
            "label": "Idle",
            "error": None,
            "running": False,
        }
    flow = ctx.flow_kind.value
    state = ctx.state.value
    return {
        "flow": flow,
        "state": state,
        "label": _label_for(flow, state),
        "error": ctx.error,
        "running": running,
    }


@router.post("/start-install")
def start_install() -> dict:
    if engine._engine.is_running:
        raise HTTPException(status_code=409, detail="An automation flow is already running")
    vm_password = ensure_vm_password()
    ctx = AutomationContext(
        flow_kind=FlowKind.INSTALL,
        vm_password=vm_password,
    )
    engine.start_flow(ctx, sse.broadcast)
    return {"status": "started"}


@router.get("/credentials-preset")
def credentials_preset() -> dict:
    """Return whether Apple ID credentials are pre-configured server-side.

    The UI uses this on load to decide whether to require manual entry.
    """
    return {"preset": bool(APPLE_EMAIL and APPLE_PASSWORD)}


class RuntimeStartBody(BaseModel):
    apple_email: str = ""
    apple_password: str = ""
    iphone_passcode: str = ""
    restore_golden: bool = True
    icloud_sync_timeout_s: int = 1800


@router.post("/start-runtime")
def start_runtime(body: RuntimeStartBody) -> dict:
    if engine._engine.is_running:
        raise HTTPException(status_code=409, detail="An automation flow is already running")
    adapter = get_active_adapter()
    if body.restore_golden and not adapter.golden_image_path(VM_DIR).exists():
        raise HTTPException(
            status_code=400,
            detail=(
                f"No golden image found for {adapter.display_name}. "
                "Run the install flow first."
            ),
        )
    email, password = _resolve_credentials(body.apple_email, body.apple_password)
    vm_password = ensure_vm_password()
    ctx = AutomationContext(
        flow_kind=FlowKind.RUNTIME,
        vm_password=vm_password,
        apple_email=email,
        apple_password=password,
        iphone_passcode=body.iphone_passcode or IPHONE_PASSCODE,
        restore_golden=body.restore_golden,
        icloud_sync_timeout_s=body.icloud_sync_timeout_s,
    )
    engine.start_flow(ctx, sse.broadcast)
    return {"status": "started"}


@router.post("/resume-install")
def resume_install(state: str = "waiting_install") -> dict:
    """Resume the install flow from a specific state.

    Useful after an error — for example, to resume monitoring once the
    macOS installer is already running (use state='waiting_install') or
    to continue from the post-install reboot (state='booting_installed').
    """
    if engine._engine.is_running:
        raise HTTPException(status_code=409, detail="An automation flow is already running")
    try:
        initial = InstallState(state)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown install state: {state!r}")
    vm_password = ensure_vm_password()
    ctx = AutomationContext(
        flow_kind=FlowKind.INSTALL,
        vm_password=vm_password,
        initial_state=initial,
    )
    engine.start_flow(ctx, sse.broadcast)
    return {"status": "resumed", "from_state": state}


@router.post("/resume-runtime")
def resume_runtime(body: RuntimeStartBody, state: str = "waiting_login_screen") -> dict:
    """Resume the runtime flow from a specific state.

    Useful after an error — the VM may already be running past the failed
    state.  Example: if picker_selecting failed but macOS is booting, use
    state='waiting_login_screen'."""
    if engine._engine.is_running:
        raise HTTPException(status_code=409, detail="An automation flow is already running")
    try:
        initial = RuntimeState(state)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown runtime state: {state!r}")
    email, password = _resolve_credentials(body.apple_email, body.apple_password)
    vm_password = ensure_vm_password()
    ctx = AutomationContext(
        flow_kind=FlowKind.RUNTIME,
        vm_password=vm_password,
        apple_email=email,
        apple_password=password,
        iphone_passcode=body.iphone_passcode or IPHONE_PASSCODE,
        restore_golden=False,
        icloud_sync_timeout_s=body.icloud_sync_timeout_s,
        initial_state=initial,
    )
    engine.start_flow(ctx, sse.broadcast)
    return {"status": "resumed", "from_state": state}


@router.post("/abort")
def abort_flow() -> dict:
    engine.abort()
    return {"status": "aborted"}
