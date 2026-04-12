"""VM driver abstraction for the wizard.

The wizard needs a small surface: send keys, type text, click, screenshot,
OCR, detect the current screen, and restart QEMU.  This module defines
the :class:`VMDriver` protocol and :class:`TrackerVMDriver`, an adapter
that is constructed with explicit callables so it is not coupled to
``server.tracker``'s global state (and therefore works under both the
``server.`` package layout used by tests and the flat ``wizard/`` layout
used by the Nix install — see ``server/package.nix``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class VMDriver(Protocol):
    def send_key(self, key: str, delay: float = 0.15) -> None: ...
    def type_text(self, text: str) -> None: ...
    def click(self, x: int, y: int, delay: float = 0.5) -> None: ...
    def screenshot(self) -> bytes | None: ...
    def ocr_region(self, ppm: bytes | None, x1: int, y1: int,
                   x2: int, y2: int) -> str: ...
    def detect_screen(self, ppm: bytes | None) -> str: ...
    def restart_without_mac_hdd(self) -> bool: ...
    def restart_with_mac_hdd(self) -> bool: ...
    def kill(self) -> None: ...


@dataclass
class TrackerVMDriver:
    """Production driver: each method is a plain delegate to a tracker
    helper.  ``server.tracker`` constructs one of these in
    ``_bypass_setup_assistant_via_wizard`` and passes its own helpers
    in; no import of tracker happens inside this module.
    """
    _send_key: Callable[[str, float], None]
    _type_text: Callable[[str], None]
    _mouse_click: Callable[[int, int, float], None]
    _take_screenshot: Callable[[], bytes | None]
    _ocr_region: Callable[[bytes | None, int, int, int, int], str]
    _detect_screen: Callable[[bytes | None], str]
    _restart_setup_vm: Callable[[bool], bool]
    _kill_vm: Callable[[], None]

    def send_key(self, key, delay=0.15):
        self._send_key(key, delay)

    def type_text(self, text):
        self._type_text(text)

    def click(self, x, y, delay=0.5):
        self._mouse_click(x, y, delay)

    def screenshot(self):
        return self._take_screenshot()

    def ocr_region(self, ppm, x1, y1, x2, y2):
        return self._ocr_region(ppm, x1, y1, x2, y2)

    def detect_screen(self, ppm):
        return self._detect_screen(ppm)

    def restart_without_mac_hdd(self):
        return self._restart_setup_vm(False)

    def restart_with_mac_hdd(self):
        return self._restart_setup_vm(True)

    def kill(self):
        self._kill_vm()
