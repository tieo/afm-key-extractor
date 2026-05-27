"""Periodic runtime flow scheduler.

When AIRTAG_AUTO_RUN=true, triggers a fresh extraction run every
AIRTAG_POLL_INTERVAL seconds (default 900 = 15 min), provided:
  - no flow is currently running
  - a golden image exists
  - Apple ID credentials are configured
"""

from __future__ import annotations

import asyncio

from .config import (
    APPLE_EMAIL,
    APPLE_PASSWORD,
    IPHONE_PASSCODE,
    MACOS_VERSION,
    POLL_INTERVAL,
    VM_DIR,
)
from .events import emit


async def run(stop_event: asyncio.Event) -> None:
    emit("info", "scheduler", f"Auto-run enabled — interval {POLL_INTERVAL}s")
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        await _maybe_start_run()


async def _maybe_start_run() -> None:
    from .automation import engine
    from .automation.context import AutomationContext
    from .automation.states import FlowKind
    from .macos_adapter import get_adapter
    from .vm_password import ensure as ensure_vm_password

    if engine._engine.is_running:
        emit("info", "scheduler", "Skipping scheduled run — flow already running")
        return

    if not APPLE_EMAIL or not APPLE_PASSWORD:
        emit("warning", "scheduler",
             "Skipping scheduled run — AIRTAG_APPLE_EMAIL / AIRTAG_APPLE_PASSWORD not set")
        return

    adapter = get_adapter(MACOS_VERSION)
    if not adapter.golden_image_path(VM_DIR).exists():
        emit("warning", "scheduler",
             "Skipping scheduled run — no golden image found (run install flow first)")
        return

    emit("info", "scheduler", "Starting scheduled runtime run")
    try:
        vm_password = ensure_vm_password()
        ctx = AutomationContext(
            flow_kind=FlowKind.RUNTIME,
            vm_password=vm_password,
            apple_email=APPLE_EMAIL,
            apple_password=APPLE_PASSWORD,
            iphone_passcode=IPHONE_PASSCODE,
            restore_golden=True,
        )
        from .api import sse
        engine.start_flow(ctx, sse.broadcast)
    except Exception as exc:
        emit("error", "scheduler", f"Failed to start scheduled run: {exc}")
