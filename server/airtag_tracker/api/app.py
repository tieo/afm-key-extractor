"""FastAPI application factory for the AirTag Key Extractor UI."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import AUTO_RUN, DATA_DIR, KEYS_DIR, STATIC_DIR, VM_ENABLED
from ..events import emit, set_broadcast_hook
from . import sse
from .routers import automation, debug, events, keys, setup, twofa


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Capture the running event loop for SSE broadcast from sync threads.
    sse.set_event_loop(asyncio.get_running_loop())
    # Wire events.emit → SSE so log entries stream in real time.
    set_broadcast_hook(sse.broadcast)

    # Ensure data directories exist.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except Exception:
        pass
    KEYS_DIR.mkdir(parents=True, exist_ok=True)

    # Kill any QEMU process left over from a previous server instance.
    # QEMU runs with -daemonize so it survives container restarts; without
    # this the automation engine would start a fresh install against a VM
    # that's already mid-install from a prior (now dead) server process.
    if VM_ENABLED:
        from .. import vm as _vm
        if _vm.is_running():
            emit("warning", "system",
                 "Orphaned QEMU process found on startup — stopping it")
            try:
                _vm.stop()
            except Exception as _e:
                emit("warning", "system", f"Failed to stop orphaned QEMU: {_e}")

    emit("info", "system", "AirTag Key Extractor API started")

    scheduler_stop = asyncio.Event()
    scheduler_task = None
    if AUTO_RUN and VM_ENABLED:
        from .. import scheduler
        scheduler_task = asyncio.create_task(scheduler.run(scheduler_stop))

    yield

    if scheduler_task is not None:
        scheduler_stop.set()
        scheduler_task.cancel()

    emit("info", "system", "AirTag Key Extractor API shutting down")


def create_app() -> FastAPI:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title="AirTag Key Extractor",
        version="1.0.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:8042",
            "http://127.0.0.1:8042",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routers (imported as router objects from routers/__init__.py).
    app.include_router(automation)
    app.include_router(twofa)
    app.include_router(keys)
    app.include_router(events)
    app.include_router(debug)
    app.include_router(setup)

    # Static files (CSS, JS, …).
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Also serve static assets directly from root paths that the HTML references
    # (e.g. /css/app.css, /js/init.js).
    for sub in ("css", "js"):
        sub_dir = STATIC_DIR / sub
        if sub_dir.exists():
            app.mount(f"/{sub}", StaticFiles(directory=str(sub_dir)), name=sub)

    # Root — serve the SPA index.
    index_html = STATIC_DIR / "index.html"

    @app.get("/")
    async def root():
        return FileResponse(str(index_html))

    return app
