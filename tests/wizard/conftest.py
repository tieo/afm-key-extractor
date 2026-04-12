"""Shared fakes for the wizard test suite.

The wizard is designed around two injectable interfaces: :class:`VMDriver`
and :class:`Reporter`.  The fakes here let us drive the Recovery bypass
entirely in-process:

- :class:`FakeVMDriver` records every call, and serves pre-scripted
  screenshot / OCR responses per call so we can simulate different
  Recovery outcomes (success, sysadminctl fail, missing sentinel, etc.).
- The tests import :class:`CapturingReporter` from the module under test.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Make ``server`` importable from the repo root without installing.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class FakeClock:
    """Virtual clock for deterministic polling tests.

    ``sleep`` advances the clock instantly; ``now`` returns the
    advanced time.  Polling loops that use these will therefore
    terminate in finite iterations without real wall-clock waiting.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, s: float) -> None:
        self.t += s

    def now(self) -> float:
        return self.t


@dataclass
class FakeVMDriver:
    """Scriptable VMDriver for tests.

    Screens / OCR are returned in FIFO order; if the test exhausts the
    script, the last entry is repeated (so "always desktop" is one
    entry, not N).  Calls to send_key / type_text / click / restart are
    recorded on ``calls``.
    """
    screens: list[str] = field(default_factory=list)
    ocr_responses: list[str] = field(default_factory=list)
    restart_without_ok: bool = True
    restart_with_ok: bool = True
    calls: list[tuple] = field(default_factory=list)

    def _pop(self, lst: list[str], default: str) -> str:
        if not lst:
            return default
        if len(lst) == 1:
            return lst[0]
        return lst.pop(0)

    def send_key(self, key, delay=0.15):
        self.calls.append(("send_key", key, delay))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def click(self, x, y, delay=0.5):
        self.calls.append(("click", x, y, delay))

    def screenshot(self):
        self.calls.append(("screenshot",))
        return b"FAKEPPM"

    def ocr_region(self, ppm, x1, y1, x2, y2):
        self.calls.append(("ocr_region", x1, y1, x2, y2))
        return self._pop(self.ocr_responses, "")

    def detect_screen(self, ppm):
        self.calls.append(("detect_screen",))
        return self._pop(self.screens, "unknown")

    def restart_without_mac_hdd(self):
        self.calls.append(("restart_without_mac_hdd",))
        return self.restart_without_ok

    def restart_with_mac_hdd(self):
        self.calls.append(("restart_with_mac_hdd",))
        return self.restart_with_ok

    def kill(self):
        self.calls.append(("kill",))
