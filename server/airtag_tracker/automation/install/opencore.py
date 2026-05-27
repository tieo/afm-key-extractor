"""OpenCore picker interaction handlers.

Covers the picker at three distinct moments in the install flow:
1. First boot into Recovery (wait_for_picker + select_installer).
2. Recovery environment loading (wait_for_recovery).
3. Post-install boot into the freshly installed macOS (select_installed).
"""

from __future__ import annotations

import time

from ... import qmp, vm, vm_ui
from ...config import VM_DIR
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from ..wait import wait_until
from .. import screen


def _wait_qemu_up(deadline_s: float = 30.0) -> None:
    """Block until QEMU answers QMP, or the deadline expires.

    Replaces ``time.sleep(10.0)`` after ``vm.start()`` — returns as soon as
    QMP is reachable instead of waiting a fixed worst-case duration.
    """
    if not wait_until(vm.is_running, deadline_s=deadline_s, poll_s=0.5):
        emit("warning", "opencore",
             f"QEMU did not answer QMP within {deadline_s}s after start")


def wait_for_picker(ctx: AutomationContext) -> InstallState:
    """Poll until the OpenCore boot picker is visible.

    Starts the VM in install mode if it is not already running.
    Uses template matching as the primary signal, OCR ("EFI") as fallback.
    Deadline: 180 s after VM start.  Raises RuntimeError on timeout.
    """
    if vm.is_running():
        emit("info", "opencore", "VM already running — stopping before fresh install")
        vm.stop()
        time.sleep(3.0)  # let QEMU terminate cleanly
    emit("info", "opencore", "Starting VM in install mode")
    vm.start_for_install(base_system=ctx.adapter.base_system_path(VM_DIR))
    time.sleep(5.0)  # give QEMU a moment to initialise before polling

    # 60 s: QEMU launch (~27s) + OVMF POST → OpenCore appears in <5s.
    # mac_hdd_ng.img is always blank at this point so OVMF has nothing to probe.
    deadline_s = 60
    poll_s = 3.0
    t0 = time.time()
    emit("info", "opencore", "Waiting for OpenCore picker…")
    while time.time() - t0 < deadline_s:
        if screen.detect_opencore_picker():
            emit("info", "opencore", "OpenCore picker detected")
            return InstallState.PICKER_SELECTING
        time.sleep(poll_s)
    raise RuntimeError(
        f"OpenCore picker not detected within {deadline_s}s"
    )


def select_installer(ctx: AutomationContext) -> InstallState:
    """Navigate the picker to the macOS installer entry and confirm.

    The macOS Installer (BaseSystem) entry is immediately to the right of
    the default EFI entry.  Send right, wait for the selection to register,
    then send ret.  Batching both keys caused a race where ret fired before
    the picker finished processing right, leaving EFI selected and Recovery
    never loading.
    """
    emit("info", "opencore", "Selecting installer entry (right + ret)")
    with ctx.qmp_lock:
        qmp.send_keys(["right"])
        time.sleep(0.5)
        qmp.send_keys(["ret"])
    return InstallState.WAITING_RECOVERY


def wait_for_recovery(ctx: AutomationContext) -> InstallState:
    """Poll until the macOS Recovery Utilities screen is visible.

    Looks for both "Reinstall macOS" and "Disk Utility" in the OCR output.
    Deadline: 150 s.  Raises RuntimeError on timeout.
    """
    deadline_s = 150
    poll_s = 4.0
    progress_interval_s = 30
    t0 = time.time()
    last_progress = t0
    emit("info", "opencore", "Waiting for Recovery Utilities screen…")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            screen_snippet = vm_ui.screen_text()[:80] if hasattr(vm_ui, 'screen_text') else ''
            emit("info", "opencore",
                 f"Still waiting for Recovery Utilities… ({elapsed:.0f}s) screen: {repr(screen_snippet)}")
            last_progress = now
        if screen.detect_recovery_utilities():
            emit("info", "opencore", "Recovery Utilities screen detected")
            return InstallState.FORMAT_DISK
        time.sleep(poll_s)
    raise RuntimeError(
        f"Recovery Utilities screen not detected within {deadline_s}s"
    )


def select_macos_entry(ctx: AutomationContext) -> None:
    """Navigate to the macOS entry using OCR-detected picker position.

    OpenCore's builtin picker ignores mouse events — only keyboard works.
    We OCR the picker to find where the macOS label appears among visible
    entries, count the distinct entry columns to its left, and press Right
    that many times before confirming with Enter.

    If OCR finds no macOS label, falls back to right+ret (assumes MacHDD is
    at picker position 1, which is true in both install and normal mode after
    macOS writes itself to disk).
    """
    p = vm_ui._screendump()
    sw, sh = vm_ui._screen_size(p)
    words = vm_ui.ocr_words(p)  # deletes p when done

    # Locate the macOS entry label and its x-center.
    macos_x: int | None = None
    macos_label = ""
    for label in ("Macintosh", "macOS", "Mac"):
        hit = vm_ui.find_phrase(words, label, screen_h=sh, exclude_chrome=False)
        if hit:
            macos_x, _ = hit
            macos_label = label
            break

    if macos_x is None:
        emit("warning", "opencore",
             f"macOS label not found — words: {[w[0] for w in words[:20]]}"
             "; pressing right+ret (assume MacHDD at position 1)")
        with ctx.qmp_lock:
            qmp.send_keys(["right"])
            time.sleep(0.5)
            qmp.send_keys(["ret"])
        return

    # Count distinct entry clusters to the left of the macOS label.
    # The picker lays entries out horizontally; each cluster of OCR words
    # that share a similar x-center corresponds to one picker entry column.
    # Words in the version string band at the very bottom are excluded.
    band_lo = sh * 0.25
    band_hi = sh * 0.77
    CLUSTER_PX = 150  # words within this distance merge into one cluster

    clusters: list[int] = []  # x-centers of clusters found to the left
    for _, wx, wy, ww, wh in words:
        if not (band_lo <= wy <= band_hi):
            continue
        cx = wx + ww // 2
        if cx >= macos_x - 80:
            continue  # this word belongs to macOS entry or is to its right
        merged = any(abs(cx - ec) <= CLUSTER_PX for ec in clusters)
        if not merged:
            clusters.append(cx)

    n_rights = len(clusters)

    # Geometry fallback: OCR often misses short labels like "EFI" (3 chars).
    # In a 2-entry picker (EFI | MacHDD), MacHDD sits at ~56% of screen width.
    # In a 1-entry picker MacHDD is centered at ~50%.
    # If OCR found 0 clusters but MacHDD is noticeably right of center,
    # infer 1 entry to its left (the EFI entry OCR missed).
    if n_rights == 0 and macos_x > sw * 0.52:
        n_rights = 1
        emit("info", "opencore",
             f"OCR found 0 left clusters but '{macos_label}' at x≈{macos_x}px "
             f"({macos_x/sw:.2f} of screen width) — inferring 1 entry to left")

    emit("info", "opencore",
         f"OCR: '{macos_label}' at x≈{macos_x}px, {len(clusters)} OCR-cluster"
         f"{'s' if len(clusters) != 1 else ''} to left, pressing {'right+' * n_rights}ret")
    with ctx.qmp_lock:
        for _ in range(n_rights):
            qmp.send_keys(["right"])
            time.sleep(0.5)
        qmp.send_keys(["ret"])


def select_installed(ctx: AutomationContext) -> InstallState:
    """Navigate the post-install OpenCore picker to the Macintosh HD entry.

    macOS installation involves two configure phases, each preceded by an
    OpenCore picker.  The handler loops, selecting MacHDD each time the
    picker appears, and exits when Setup Assistant is detected.

    OVMF boot failure recovery
    --------------------------
    macOS writes *persistent* EFI variables to OVMF's in-memory pflash
    during configure phases.  After phase 1 reboots, OVMF tries those
    stale entries, fails all of them, and falls into the Boot Maintenance
    Manager (BdsDxe → PXE → Boot Manager).  system_reset does NOT help
    because QEMU keeps the in-memory pflash state across resets.

    The only reliable fix: kill and restart QEMU.  QEMU then re-reads
    OVMF_VARS from disk (still the original clean file — QEMU is killed
    before it ever flushes VARS to disk), which has OpenCore in the boot
    order.  OpenCore loads and the picker appears.

    Picker entry count by QEMU mode
    --------------------------------
    Install mode (BaseSystem.img attached):  EFI | BaseSystem | MacHDD
        → right+right+ret selects MacHDD
    Normal mode (no BaseSystem.img):         EFI | MacHDD
        → right+ret selects MacHDD

    The first picker is always in install mode.  After any QEMU restart
    we switch to normal mode.
    """
    deadline_s = 3600  # 1 h: covers two configure phases + OVMF recovery
    poll_s = 5.0
    progress_interval_s = 60
    t0 = time.time()
    last_progress = t0
    picker_seen = 0
    qemu_restarts = 0
    MAX_QEMU_RESTARTS = 5
    # First boot is always install mode (BaseSystem.img still attached).
    # After a QEMU restart we use normal mode (no BaseSystem.img).
    in_install_mode = True

    # Start VM if it stopped (e.g. container was rebuilt while install ran).
    # Use normal mode so only EFI and MacHDD appear in the picker.
    if not vm.is_running():
        emit("info", "opencore", "VM not running at select_installed entry — starting in normal mode")
        vm.start()
        in_install_mode = False
        _wait_qemu_up()

    emit("info", "opencore", "Waiting for post-install boot sequence…")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            screen_snippet = vm_ui.screen_text()[:80] if hasattr(vm_ui, 'screen_text') else ''
            emit("info", "opencore",
                 f"Still waiting for Setup Assistant… ({elapsed:.0f}s) "
                 f"pickers={picker_seen} restarts={qemu_restarts} "
                 f"screen: {repr(screen_snippet)}")
            last_progress = now
        if screen.detect_opencore_picker():
            picker_seen += 1
            emit("info", "opencore",
                 f"Post-install picker #{picker_seen} ({'install' if in_install_mode else 'normal'} mode)"
                 " — selecting macOS entry")
            select_macos_entry(ctx)
            time.sleep(10.0)  # settle: post-picker boot animation, no actionable signal
            continue

        if screen.detect_setup_assistant():
            emit("info", "opencore", "Setup Assistant detected — advancing flow")
            return InstallState.SA_COUNTRY

        # SA completed before this handler ran (e.g. resumed after QEMU was
        # killed mid-SA and rebooted to the login screen).  Skip SA and go
        # straight to the finalize stage.
        if screen.detect_login_screen() or screen.detect_desktop():
            emit("info", "opencore",
                 "Login screen/desktop detected — SA already complete, skipping to finalize")
            return InstallState.DISMISS_FIRST_BOOT

        if screen.detect_recovery_utilities() \
                and qemu_restarts < MAX_QEMU_RESTARTS:
            # Booted to Recovery instead of macOS — picker navigation landed on
            # BaseSystem/Recovery by mistake.  Restart QEMU in normal mode
            # (no BaseSystem) so the picker has only EFI and MacHDD.
            qemu_restarts += 1
            in_install_mode = False
            emit("warning", "opencore",
                 f"Recovery Utilities appeared after picker #{picker_seen}"
                 f" — wrong entry selected; restarting QEMU in normal mode (restart #{qemu_restarts})")
            vm.stop()
            wait_until(lambda: not vm.is_running(), deadline_s=20.0, poll_s=0.5)
            vm.start()
            _wait_qemu_up()
            continue

        if screen.detect_tiano_bios() and picker_seen >= 1 \
                and qemu_restarts < MAX_QEMU_RESTARTS:
            # OVMF can't boot after macOS wrote persistent EFI vars.
            # system_reset doesn't clear in-memory pflash — must restart QEMU.
            qemu_restarts += 1
            in_install_mode = False
            emit("info", "opencore",
                 f"OVMF boot failure after picker #{picker_seen}"
                 f" — restarting QEMU (restart #{qemu_restarts})")
            vm.stop()
            wait_until(lambda: not vm.is_running(), deadline_s=20.0, poll_s=0.5)
            vm.start()
            _wait_qemu_up()
            continue

        time.sleep(poll_s)
    raise RuntimeError(
        f"Setup Assistant not reached within {deadline_s}s after install"
    )
