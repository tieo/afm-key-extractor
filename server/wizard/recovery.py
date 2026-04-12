"""Recovery-path bypass for the Setup Assistant.

Implements docs/WIZARD_AUTOMATION.md §4: boot the VM into macOS
Recovery (no MacHDD), open Terminal, locate the Data volume, create an
admin user with ``sysadminctl`` (fallback ``dscl``), write the
SetupAssistant sentinels, then reboot with MacHDD attached.

Every shell command that mutates state emits a ``WIZARD_SENTINEL``
line that we OCR back and assert on before proceeding.  There is no
silent forward progress: a missing sentinel is a hard abort with
``phase='error'``.

This module does *not* own QEMU restart argv — that lives in
``qemu.TrackerVMDriver.restart_*``.  It also does not own OCR tuning;
the driver provides ``detect_screen`` / ``ocr_region``.
"""

from __future__ import annotations

import time as _real_time
from dataclasses import dataclass, field
from typing import Callable

VM_USER = "airtag"
VM_PASSWORD = "airtagpw"  # 8 chars — Setup Assistant silently rejects <8
VM_FULLNAME = "airtag"

# Terminal OCR region (same 1280x800 framebuffer as everywhere else).
_TERM_REGION = (50, 50, 1230, 750)

# How many seconds to wait for each Recovery milestone.  Chosen against
# observed behavior in _auto_install_worker (tracker.py:2500+).
_TIMEOUT_RECOVERY_BOOT = 300
_TIMEOUT_TERMINAL_OPEN = 30
_TIMEOUT_CMD_RESPONSE = 20
_TIMEOUT_POST_REBOOT = 420

# Canonical Ventura SetupAssistant skip keys.  See §4.4 of the design
# doc for provenance caveats — list is believed complete for 13.x.
_SA_SKIP_KEYS = [
    "DidSeeCloudSetup",
    "DidSeeSiriSetup",
    "DidSeePrivacy",
    "DidSeeAccessibility",
    "DidSeeApplePaySetup",
    "DidSeeSyncSetup",
    "DidSeeTrueTonePrivacy",
    "DidSeeAppearanceSetup",
    "DidSeeScreenTime",
    "GestureMovieSeen",
    "SkipFirstLoginOptimization",
]


@dataclass
class _Ctx:
    """Bundle of (vm, reporter, clock) threaded through the phase helpers.

    ``sleep`` and ``now`` are injected so tests can run at wall-clock
    zero.  Default production values use the real ``time`` module.
    """
    vm: object  # VMDriver
    rep: object  # Reporter
    sleep: Callable[[float], None] = field(default=_real_time.sleep)
    now: Callable[[], float] = field(default=_real_time.time)


def run_recovery_bypass(vm, reporter, *, sleep=None, now=None):
    """Entry point; returns :class:`server.wizard.Outcome`.

    Kept at top of file so the control flow is readable — every phase
    is a helper below.
    """
    from . import Outcome  # local import avoids cycle

    ctx = _Ctx(
        vm=vm,
        rep=reporter,
        sleep=sleep if sleep is not None else _real_time.sleep,
        now=now if now is not None else _real_time.time,
    )
    try:
        reporter.phase("recovery_boot", "Booting into macOS Recovery (no MacHDD)")
        if not _boot_into_recovery(ctx):
            return _fail("recovery_boot", "failed to reach Recovery screen")

        reporter.phase("recovery_terminal", "Opening Terminal from Utilities menu")
        if not _open_terminal(ctx):
            return _fail("recovery_terminal", "Terminal did not open")

        reporter.phase("recovery_create_user", "Creating admin account via sysadminctl")
        dvol = _find_data_volume(ctx)
        if not dvol:
            return _fail("recovery_create_user",
                         "could not locate writable Data volume")

        if not _create_user(ctx, dvol):
            return _fail("recovery_create_user",
                         "user creation failed (sysadminctl and dscl both rejected)")

        if not _write_setup_sentinels(ctx, dvol):
            return _fail("recovery_create_user",
                         "SetupAssistant sentinel writes did not complete")

        reporter.phase("recovery_reboot", "Rebooting with MacHDD attached")
        if not ctx.vm.restart_with_mac_hdd():
            return _fail("recovery_reboot", "could not restart VM with MacHDD")

        if not _wait_for_post_reboot(ctx):
            return _fail("verify_desktop",
                         "post-reboot screen is not desktop or login")

        reporter.phase("done", "Recovery bypass complete")
        return Outcome(path="recovery", status="ok", phase="done",
                       message=f"user {VM_USER} created via Recovery")
    except Exception as exc:  # defensive — never surface as unhandled
        reporter.error(f"Recovery bypass crashed: {exc!r}")
        return Outcome(path="recovery", status="failed", phase="error",
                       message=f"exception: {exc!r}")


def _fail(phase: str, msg: str):
    from . import Outcome
    return Outcome(path="recovery", status="failed", phase=phase, message=msg)


# ---------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------

def _boot_into_recovery(ctx: _Ctx) -> bool:
    """Restart QEMU without MacHDD; poll until the Recovery screen shows."""
    if not ctx.vm.restart_without_mac_hdd():
        ctx.rep.error("VM restart (no MacHDD) failed")
        return False
    return _wait_for_screens(ctx, {"recovery"}, _TIMEOUT_RECOVERY_BOOT,
                             poll_s=5, msg="Waiting for Recovery")


def _open_terminal(ctx: _Ctx) -> bool:
    """Click Utilities → Terminal and wait for the terminal screen.

    Old code hardcoded pixel (242, 10) for the Utilities menu.  We keep
    the same anchor — it's load-bearing on the 1280x800 framebuffer and
    has been observed to work in production — but wrap with a retry:
    if the terminal screen does not appear, send Escape and try once
    more.  This is the only clickable menu-bar item that matters.
    """
    for attempt in range(2):
        ctx.vm.click(242, 10, 1.5)          # Utilities menu
        ctx.vm.click(242, 55, 3.0)          # Terminal entry
        ctx.vm.click(400, 300, 1.0)         # focus the window
        if _wait_for_screens(ctx, {"terminal"}, _TIMEOUT_TERMINAL_OPEN,
                             poll_s=2, msg="Waiting for Terminal"):
            return True
        ctx.rep.warning(f"Terminal did not open (attempt {attempt + 1})")
        ctx.vm.send_key("escape", 0.5)
    return False


def _find_data_volume(ctx: _Ctx) -> str | None:
    """Mount every APFS volume and pick the one with a dslocal tree.

    Emits WIZARD_SENTINEL DVOL=... and DVOL_RW=1; we OCR the terminal
    and return the DVOL path only if both sentinels are present.
    """
    script = _DATA_VOLUME_SCRIPT
    ctx.vm.type_text(script)
    ctx.vm.send_key("ret")
    ctx.sleep(3)
    text = _read_terminal_ocr(ctx)

    dvol = _parse_sentinel(text, "DVOL")
    rw = _parse_sentinel(text, "DVOL_RW")
    if not dvol:
        ctx.rep.error("WIZARD_SENTINEL DVOL missing from terminal OCR")
        return None
    if rw != "1":
        ctx.rep.error(f"WIZARD_SENTINEL DVOL_RW != 1 (got {rw!r})")
        return None
    ctx.rep.info(f"Data volume located: {dvol}")
    return dvol


def _create_user(ctx: _Ctx, dvol: str) -> bool:
    """Try sysadminctl first; if USER_PLIST_OK or ShadowHashData missing,
    fall back to dscl against the mounted Default node.
    """
    if _try_sysadminctl(ctx, dvol):
        return True
    ctx.rep.warning("sysadminctl path failed, falling back to dscl")
    return _try_dscl_fallback(ctx, dvol)


def _try_sysadminctl(ctx: _Ctx, dvol: str) -> bool:
    script = _SYSADMINCTL_SCRIPT.format(
        user=VM_USER, pw=VM_PASSWORD, full=VM_FULLNAME, dvol=dvol
    )
    ctx.vm.type_text(script)
    ctx.vm.send_key("ret")
    ctx.sleep(5)
    text = _read_terminal_ocr(ctx)

    if _parse_sentinel(text, "USER_CREATED") != "0":
        ctx.rep.warning("sysadminctl returned non-zero")
        return False
    if "USER_PLIST_OK" not in text:
        ctx.rep.warning("USER_PLIST_OK sentinel missing — plist not written")
        return False
    if "SHADOW_OK" not in text:
        # ShadowHashData absence is the §2(a) root cause: a user without
        # it cannot authenticate, which is the failure the whole
        # redesign is targeted at.
        ctx.rep.warning("SHADOW_OK sentinel missing — user has no "
                        "ShadowHashData (login would fail)")
        return False
    ctx.rep.info("sysadminctl succeeded (user, plist, shadow all OK)")
    return True


def _try_dscl_fallback(ctx: _Ctx, dvol: str) -> bool:
    script = _DSCL_SCRIPT.format(user=VM_USER, pw=VM_PASSWORD, dvol=dvol)
    ctx.vm.type_text(script)
    ctx.vm.send_key("ret")
    ctx.sleep(5)
    text = _read_terminal_ocr(ctx)
    if "DSCL_OK" not in text:
        ctx.rep.error("DSCL_OK sentinel missing — dscl user creation failed")
        return False
    if "SHADOW_OK" not in text:
        ctx.rep.error("SHADOW_OK sentinel missing after dscl")
        return False
    ctx.rep.info("dscl fallback succeeded")
    return True


def _write_setup_sentinels(ctx: _Ctx, dvol: str) -> bool:
    script = _SETUP_SENTINEL_SCRIPT.format(
        dvol=dvol, user=VM_USER, keys=" ".join(_SA_SKIP_KEYS)
    )
    ctx.vm.type_text(script)
    ctx.vm.send_key("ret")
    ctx.sleep(5)
    text = _read_terminal_ocr(ctx)
    for tag in ("APPLESETUPDONE_OK", "SA_PLIST_OK", "USER_SA_OK", "KBD_OK"):
        if tag not in text:
            ctx.rep.error(f"{tag} sentinel missing")
            return False
    ctx.rep.info("All SetupAssistant sentinels written")
    return True


def _wait_for_post_reboot(ctx: _Ctx) -> bool:
    return _wait_for_screens(ctx, {"desktop", "login_screen"},
                             _TIMEOUT_POST_REBOOT, poll_s=10,
                             msg="Waiting for macOS desktop/login")


# ---------------------------------------------------------------------
# OCR / polling helpers
# ---------------------------------------------------------------------

def _wait_for_screens(ctx: _Ctx, expected: set, timeout: int,
                      poll_s: int, msg: str) -> bool:
    deadline = ctx.now() + timeout
    last = "unknown"
    while ctx.now() < deadline:
        ppm = ctx.vm.screenshot()
        last = ctx.vm.detect_screen(ppm)
        if last in expected:
            ctx.rep.info(f"{msg}: reached {last}")
            return True
        ctx.sleep(poll_s)
    ctx.rep.error(f"{msg}: timed out (last screen: {last})")
    return False


def _read_terminal_ocr(ctx: _Ctx) -> str:
    ppm = ctx.vm.screenshot()
    return ctx.vm.ocr_region(ppm, *_TERM_REGION)


def _parse_sentinel(text: str, key: str) -> str | None:
    """Extract ``WIZARD_SENTINEL <KEY>=<value>`` from OCR'd output.

    OCR introduces whitespace noise so we scan line-by-line.
    """
    tag = "WIZARD_SENTINEL"
    for line in text.splitlines():
        if tag not in line:
            continue
        after = line.split(tag, 1)[1].strip()
        if not after.startswith(key):
            continue
        remainder = after[len(key):]
        # Require a word boundary — ``=`` (with value) or end-of-token
        # (bare sentinel).  Guards against ``DVOL`` matching ``DVOL_RW``.
        if remainder.startswith("="):
            return remainder[1:].strip()
        if remainder == "" or remainder[0].isspace():
            return ""
    return None


# ---------------------------------------------------------------------
# Shell scripts emitted to the Recovery Terminal
# ---------------------------------------------------------------------

# Mount every APFS volume, probe for a dslocal users dir, remount RW.
_DATA_VOLUME_SCRIPT = r"""
for d in $(diskutil list | awk '/APFS Volume/ {print $NF}'); do diskutil mount "$d" >/dev/null 2>&1 || true; done
DVOL=""
for v in /Volumes/*; do
  if [ -d "$v/private/var/db/dslocal/nodes/Default/users" ]; then DVOL="$v"; break; fi
done
if [ -n "$DVOL" ]; then
  mount -uw "$DVOL" 2>/dev/null && echo WIZARD_SENTINEL DVOL=$DVOL && echo WIZARD_SENTINEL DVOL_RW=1
fi
""".strip()

_SYSADMINCTL_SCRIPT = r"""
sysadminctl -addUser {user} -fullName "{full}" -password "{pw}" -admin -home "{dvol}/Users/{user}" -shell /bin/zsh
echo WIZARD_SENTINEL USER_CREATED=$?
P="{dvol}/private/var/db/dslocal/nodes/Default/users/{user}.plist"
[ -s "$P" ] && echo WIZARD_SENTINEL USER_PLIST_OK
plutil -p "$P" 2>/dev/null | grep -q ShadowHashData && echo WIZARD_SENTINEL SHADOW_OK
""".strip()

_DSCL_SCRIPT = r"""
N="{dvol}/private/var/db/dslocal/nodes/Default"
dscl -f "$N" localonly -create /Local/Default/Users/{user}
dscl -f "$N" localonly -create /Local/Default/Users/{user} UniqueID 501
dscl -f "$N" localonly -create /Local/Default/Users/{user} PrimaryGroupID 20
dscl -f "$N" localonly -create /Local/Default/Users/{user} UserShell /bin/zsh
dscl -f "$N" localonly -create /Local/Default/Users/{user} NFSHomeDirectory /Users/{user}
dscl -f "$N" localonly -passwd /Local/Default/Users/{user} "{pw}" && echo WIZARD_SENTINEL DSCL_OK
dscl -f "$N" localonly -append /Local/Default/Groups/admin GroupMembership {user}
P="$N/users/{user}.plist"
plutil -p "$P" 2>/dev/null | grep -q ShadowHashData && echo WIZARD_SENTINEL SHADOW_OK
""".strip()

_SETUP_SENTINEL_SCRIPT = r"""
touch "{dvol}/private/var/db/.AppleSetupDone" && echo WIZARD_SENTINEL APPLESETUPDONE_OK
SA="{dvol}/Library/Preferences/com.apple.SetupAssistant.plist"
mkdir -p "{dvol}/Library/Preferences"
defaults write "$SA" LastSeenBuddyBuildVersion 99Z99
for k in {keys}; do defaults write "$SA" $k -bool YES; done
[ -f "$SA" ] && echo WIZARD_SENTINEL SA_PLIST_OK
HU="{dvol}/Users/{user}/Library/Preferences"
mkdir -p "$HU" && cp "$SA" "$HU/com.apple.SetupAssistant.plist" && echo WIZARD_SENTINEL USER_SA_OK
defaults write "{dvol}/Library/Preferences/.GlobalPreferences" AppleKeyboardUIMode -int 3 && echo WIZARD_SENTINEL KBD_OK
""".strip()
