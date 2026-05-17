"""Background popup watcher — dismisses unexpected macOS dialogs.

Runs as a daemon thread alongside the main state machine.  Every
*POLL_INTERVAL_S* seconds it takes a screendump, OCR-scans it, and
checks against a table of known dismissible dialogs.  When a match is
found it sends the appropriate dismiss action and returns — one
dismissal per cycle to avoid cascade clicks.

The watcher never writes to AutomationContext.state; it only reads it
to decide whether to skip a cycle.  All QMP *write* operations (clicks,
key presses) go through the context's qmp_lock so they cannot interleave
with the main thread's multi-step sequences.

Starting / stopping
-------------------
Call ``start(ctx)`` when the VM reaches a state where popups can
appear.  Call ``stop()`` to set the stop flag; the thread will exit
after at most one more poll cycle.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import qmp as qmp_mod
from ..events import emit
from . import screen
from .context import AutomationContext
from .states import WATCHER_SUPPRESSED_STATES

POLL_INTERVAL_S = 8.0


@dataclass
class PopupRule:
    name: str
    # ALL keywords must appear in OCR text for the rule to fire.
    keywords: tuple[str, ...]
    # Called with (ctx, screen_text) to perform the dismissal.
    dismiss: Callable[["AutomationContext", str], None]


def _click_later_or_esc(ctx: AutomationContext, text: str) -> None:
    clicked = False
    for pair in (("Later",), ("Not", "Now"), ("Cancel",), ("Don't", "Merge"), ("Skip",)):
        if screen.click_text(*pair, tries=1):
            clicked = True
            break
    if not clicked:
        with ctx.qmp_lock:
            with qmp_mod.qmp() as c:
                c.send_keys(["esc"])


def _click_ok_or_esc(ctx: AutomationContext, text: str) -> None:
    if not screen.click_text("OK", tries=1):
        with ctx.qmp_lock:
            with qmp_mod.qmp() as c:
                c.send_keys(["esc"])


def _click_trust(ctx: AutomationContext, text: str) -> None:
    screen.click_text("Trust", tries=2)


def _click_dont_use(ctx: AutomationContext, text: str) -> None:
    if not screen.click_text("Don't", "Use", tries=1):
        with ctx.qmp_lock:
            with qmp_mod.qmp() as c:
                c.send_keys(["ret"])


def _enter_vm_password(ctx: AutomationContext, text: str) -> None:
    from .. import vm_ui
    if ctx.vm_password:
        vm_ui.paste_text(ctx.vm_password)
        time.sleep(0.3)
        with ctx.qmp_lock:
            with qmp_mod.qmp() as c:
                c.send_keys(["ret"])
    else:
        _click_later_or_esc(ctx, text)


POPUP_RULES: list[PopupRule] = [
    PopupRule(
        name="keyboard_setup_assistant",
        keywords=("keyboard setup assistant",),
        dismiss=lambda ctx, t: screen.click_text("Quit", tries=2) or _click_ok_or_esc(ctx, t),
    ),
    PopupRule(
        name="upgrade_macos",
        keywords=("upgrade to macos",),
        dismiss=_click_later_or_esc,
    ),
    PopupRule(
        name="software_update",
        keywords=("software update available",),
        dismiss=_click_later_or_esc,
    ),
    PopupRule(
        name="icloud_storage_full",
        keywords=("icloud storage",),
        dismiss=_click_later_or_esc,
    ),
    PopupRule(
        name="icloud_storage_upgrade",
        keywords=("choose a plan",),
        dismiss=_click_later_or_esc,
    ),
    PopupRule(
        name="location_services",
        keywords=("use location services", "don't use"),
        dismiss=_click_dont_use,
    ),
    PopupRule(
        name="mac_password_sheet",
        keywords=("enter your mac password",),
        dismiss=_enter_vm_password,
    ),
    PopupRule(
        name="icloud_merge",
        keywords=("keep a copy",),
        dismiss=_click_later_or_esc,
    ),
    PopupRule(
        name="messages_icloud",
        keywords=("messages in icloud",),
        dismiss=_click_later_or_esc,
    ),
    PopupRule(
        name="trust_computer",
        keywords=("trust this computer",),
        dismiss=_click_trust,
    ),
    PopupRule(
        name="find_my_location",
        keywords=("allow", "location", "find my"),
        dismiss=lambda ctx, t: screen.click_text("Allow", tries=1) or _click_ok_or_esc(ctx, t),
    ),
    PopupRule(
        name="photos_import",
        keywords=("import photos",),
        dismiss=_click_later_or_esc,
    ),
]


class PopupWatcher:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, ctx: AutomationContext) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            args=(ctx,),
            daemon=True,
            name="popup-watcher",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self, ctx: AutomationContext) -> None:
        while not self._stop.is_set():
            time.sleep(POLL_INTERVAL_S)
            if self._stop.is_set():
                break
            if ctx.state in WATCHER_SUPPRESSED_STATES:
                continue
            try:
                self._check(ctx)
            except Exception as e:
                emit("warning", "popup_watcher", f"cycle error: {e}")

    def _check(self, ctx: AutomationContext) -> None:
        try:
            from .. import vm_ui
            text = vm_ui.screen_text()
        except Exception:
            return

        for rule in POPUP_RULES:
            if all(kw.lower() in text for kw in rule.keywords):
                emit("info", "popup_watcher", f"Dismissing: {rule.name}")
                try:
                    rule.dismiss(ctx, text)
                except Exception as e:
                    emit("warning", "popup_watcher",
                         f"dismiss {rule.name} failed: {e}")
                return  # one dismissal per cycle


# Module-level singleton used by the engine.
_watcher = PopupWatcher()


def start(ctx: AutomationContext) -> None:
    _watcher.start(ctx)


def stop() -> None:
    _watcher.stop()
