"""Generic poll-until-condition helpers.

Most handlers need to wait for *some condition* before continuing — VM up,
screen text rendered, file populated.  Use these instead of fixed
``time.sleep`` so the wait ends as soon as the condition is true rather
than after a hardcoded duration.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def wait_until(
    predicate: Callable[[], bool],
    *,
    deadline_s: float,
    poll_s: float = 1.0,
) -> bool:
    """Return True as soon as *predicate()* returns True, or False on timeout.

    ``predicate`` is called repeatedly every *poll_s* seconds.  Exceptions are
    swallowed (treated as a falsy result) so callers don't need to wrap their
    checks in try/except.
    """
    t0 = time.monotonic()
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if time.monotonic() - t0 >= deadline_s:
            return False
        time.sleep(poll_s)


def wait_for(
    fn: Callable[[], T | None],
    *,
    deadline_s: float,
    poll_s: float = 1.0,
) -> T | None:
    """Like ``wait_until`` but returns the first truthy result instead of bool.

    Returns ``None`` on timeout.  Useful when the value you're waiting for IS
    the thing you'll use next, e.g. an OCR-detected click target.
    """
    t0 = time.monotonic()
    while True:
        try:
            value = fn()
            if value:
                return value
        except Exception:
            value = None
        if time.monotonic() - t0 >= deadline_s:
            return None
        time.sleep(poll_s)
