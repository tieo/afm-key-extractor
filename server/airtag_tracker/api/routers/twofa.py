"""Two-factor authentication relay endpoints.

Prefix: /api/vm
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...automation import engine
from ...automation.states import RuntimeState

router = APIRouter(prefix="/api/vm", tags=["2fa"])


class TwoFABody(BaseModel):
    code: str


@router.post("/apple-signin/2fa")
def deliver_2fa(body: TwoFABody) -> dict:
    ctx = engine.get_context()
    if ctx is None:
        raise HTTPException(status_code=400, detail="No active automation context")
    state = ctx.state
    if not isinstance(state, RuntimeState) or state != RuntimeState.AWAITING_2FA:
        raise HTTPException(
            status_code=400,
            detail=f"Not waiting for 2FA (current state: {state.value})",
        )
    ctx.deliver_2fa(body.code)
    return {"status": "ok"}


@router.post("/apple-signin/request-sms")
def request_sms() -> dict:
    ctx = engine.get_context()
    if ctx is None:
        raise HTTPException(status_code=400, detail="No active automation context")
    state = ctx.state
    if not isinstance(state, RuntimeState) or state != RuntimeState.AWAITING_2FA:
        raise HTTPException(
            status_code=400,
            detail=f"Not waiting for 2FA (current state: {state.value})",
        )
    ctx.request_sms()
    return {"status": "ok"}
