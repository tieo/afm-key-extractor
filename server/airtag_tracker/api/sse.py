"""SSE broadcast infrastructure.

One asyncio Queue per connected client. The ``broadcast`` function is
called from sync background threads (the automation engine) and posts
events to all client queues using ``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import Request

log = logging.getLogger("tracker.sse")

# Registry of active client queues.
_clients: list[asyncio.Queue] = []

# The running event loop — captured in the app lifespan so background
# threads can schedule coroutines onto it.
_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called from the FastAPI lifespan handler on startup."""
    global _loop
    _loop = loop


async def event_stream(request: Request) -> AsyncGenerator[str, None]:
    """Yields SSE-formatted lines. Cleans up on client disconnect."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    _clients.append(queue)
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25.0)
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                # Send a keepalive comment so proxies don't close idle connections.
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        try:
            _clients.remove(queue)
        except ValueError:
            pass


async def _put_to_all(event: dict) -> None:
    for q in list(_clients):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("SSE queue full, dropping event for one client")


def broadcast(event: dict) -> None:
    """Thread-safe broadcast — safe to call from any thread, including sync ones."""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    asyncio.run_coroutine_threadsafe(_put_to_all(event), loop)
