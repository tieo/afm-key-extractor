"""SSE event stream + log snapshot + VM status endpoints.

Prefix: /api
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ... import events as events_mod
from ... import vm
from .. import sse

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
async def sse_stream(request: Request):
    """Server-Sent Events stream. Clients receive all broadcast events."""
    return StreamingResponse(
        sse.event_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/log")
def get_log() -> list[dict]:
    """Return up to the last 500 log entries."""
    return events_mod.snapshot()


@router.get("/vm/status")
def vm_status() -> dict:
    return vm.status()
