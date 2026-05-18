"""OCR-bbox driven UI automation primitives for the macOS VM.

Why this module exists
----------------------
AppleScript automation is blocked by macOS TCC (unattended consent
prompts), and QMP send-key uses US-layout scancodes which mangle
passwords containing layout-sensitive characters. So this module is
keyboard/clipboard + bbox-click only:

* Text input goes through the VM's pasteboard (``pbcopy`` over SSH,
  ``cmd-v`` via QMP) — keymap-agnostic.
* Clicks are targeted by OCR-derived bounding boxes: screendump →
  tesseract ``--output tsv`` → find phrase → click centre. No
  hardcoded pixel coordinates anywhere.

OCR reliability
---------------
Tesseract at native VM resolution (1280×800) misses white-on-blue
button text and small UI labels. We run it on four variants —
1× / 2× × normal / inverted — and union the word boxes. That picks
up both dark-on-light body text and light-on-dark button text.

Still, OCR can fail. Every helper returns ``bool`` so callers can
retry or fall back to a keyboard action (e.g. Return for the
default button).

Settings navigation
-------------------
Never click through the sidebar to reach a pane. Every sub-pane has
a URL scheme (``x-apple.systempreferences:<bundle-id>[?<anchor>]``)
and ``open`` from SSH navigates there deterministically. See
``open_settings_pane``.
"""

from __future__ import annotations

import base64
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import qmp
from .config import QMP_SOCK
from .events import emit

VM_USER = "airtag"
VM_HOST = "localhost"
VM_PORT = 2222


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

def _ssh_password() -> str:
    from . import vm_password
    return vm_password.get() or ""


def _find_sshpass() -> str:
    """Return path to sshpass binary, searching PATH then known Nix store dirs."""
    if found := shutil.which("sshpass"):
        return found
    # Nix-deployed binary — stable enough for fallback
    for candidate in Path("/nix/store").glob("sshpass-*/bin/sshpass"):
        return str(candidate)
    return "sshpass"   # will fail with a clear FileNotFoundError


def ssh(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            _find_sshpass(), "-p", _ssh_password(),
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            "-p", str(VM_PORT),
            f"{VM_USER}@{VM_HOST}",
            cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Clipboard-based typing
# ---------------------------------------------------------------------------

def paste_text(text: str) -> None:
    """Push ``text`` to VM pasteboard, then send cmd-v.

    Bypasses QMP's keyboard-layout-dependent scancode mapping so
    arbitrary passwords / emails / codes are inserted verbatim."""
    b64 = base64.b64encode(text.encode()).decode()
    r = ssh(f"echo {shlex.quote(b64)} | base64 -D | pbcopy", timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"pbcopy failed: {(r.stderr or r.stdout).strip()[:200]}")
    time.sleep(0.3)
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "v"])
        time.sleep(0.3)


def wipe_clipboard() -> None:
    ssh("pbcopy </dev/null", timeout=10)


# ---------------------------------------------------------------------------
# Settings URL navigation
# ---------------------------------------------------------------------------

def open_settings_pane(bundle_id: str, anchor: str | None = None, settle_s: float = 6.0) -> None:
    """Navigate System Settings to a specific pane via URL scheme.

    Always kills an existing System Settings process first — if the
    app is already running, it sometimes refuses to re-navigate."""
    url = f"x-apple.systempreferences:{bundle_id}"
    if anchor:
        url += f"?{anchor}"
    # RBSRequestErrorDomain Code=5 ("cannot launch") is a launchservices
    # race that resolves after a fresh killall + short wait. Retry 3×.
    last = ""
    for attempt in range(3):
        ssh("killall 'System Settings' 2>/dev/null; true", timeout=10)
        time.sleep(2.0 if attempt == 0 else 3.0)
        r = ssh(f"open {shlex.quote(url)}", timeout=15)
        if r.returncode == 0:
            time.sleep(settle_s)
            return
        last = (r.stderr or r.stdout).strip()
    raise RuntimeError(f"open {url!r} failed after 3 attempts: {last[:200]}")


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def _screendump(path: str | None = None) -> str:
    path = path or tempfile.mktemp(suffix=".ppm")
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    qmp.screendump(path)
    return path


def _screen_size(ppm: str) -> tuple[int, int]:
    with open(ppm, "rb") as f:
        f.readline()  # magic
        line = f.readline()
        while line.startswith(b"#"):
            line = f.readline()
        w, h = map(int, line.split())
    return w, h


def _parse_tsv(text: str, scale: int) -> list[tuple[str, int, int, int, int]]:
    out: list[tuple[str, int, int, int, int]] = []
    for line in text.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12:
            continue
        try:
            conf = int(float(f[10]))
        except ValueError:
            continue
        if conf < 30:
            continue
        txt = f[11].strip()
        if not txt:
            continue
        out.append((
            txt,
            int(f[6]) // scale,
            int(f[7]) // scale,
            int(f[8]) // scale,
            int(f[9]) // scale,
        ))
    return out


def ocr_words(ppm: str) -> list[tuple[str, int, int, int, int]]:
    """OCR the framebuffer at 1×/2× and normal/inverted; union all words.

    Returns ``(text, x, y, w, h)`` tuples in native VM coordinates."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        emit("warning", "vm", "PIL unavailable — OCR disabled")
        return []
    words: list[tuple[str, int, int, int, int]] = []
    with Image.open(ppm) as im:
        im = im.convert("RGB")
        im2x = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
        # Autocontrast on 2× helps with dark-background screens (OpenCore
        # picker, login window) where flat white-on-dark fools tesseract.
        im2x_ac = ImageOps.autocontrast(im2x)
        variants = [
            (im, 1, "1x"),
            (ImageOps.invert(im), 1, "1x-inv"),
            (im2x_ac, 2, "2x"),
        ]
        for vim, scale, tag in variants:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tmp = tf.name
            vim.save(tmp)
            try:
                variants_inv = ImageOps.invert(vim) if tag == "2x" else None
                r = subprocess.run(
                    ["tesseract", tmp, "-", "tsv"],
                    capture_output=True, text=True, timeout=30,
                )
                words += _parse_tsv(r.stdout, scale)
                if variants_inv is not None:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf2:
                        tmp2 = tf2.name
                    variants_inv.save(tmp2)
                    r2 = subprocess.run(
                        ["tesseract", tmp2, "-", "tsv"],
                        capture_output=True, text=True, timeout=30,
                    )
                    words += _parse_tsv(r2.stdout, scale)
                    Path(tmp2).unlink(missing_ok=True)
            finally:
                Path(tmp).unlink(missing_ok=True)
    return words


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Vertical bands we must never click into. The menu bar holds system
# items (clock, Siri) whose text can incidentally match OCR queries;
# the Dock holds app labels, and clicking one launches that app — that
# is exactly how a click_text("Find", "Mac") matched the "Find My"
# Dock tooltip and launched Find My.app instead of toggling a settings
# row. Both bands are stable on a 1280x800 VM framebuffer.
MENUBAR_H = 28
DOCK_H = 90


def find_phrase(
    words: list[tuple[str, int, int, int, int]],
    first: str,
    last: str | None = None,
    y_tol: int = 12,
    screen_h: int | None = None,
    exclude_chrome: bool = True,
) -> tuple[int, int] | None:
    """Locate ``first`` (and optionally ``last``) in OCR output on a
    single line. Returns click centre in native pixels, or None.

    When ``exclude_chrome`` is set (the default) and ``screen_h`` is
    provided, matches that land inside the menu bar or Dock bands are
    discarded — the caller almost always means a target inside the
    app's content area."""
    def _in_content(y: int, h: int) -> bool:
        if not exclude_chrome or screen_h is None:
            return True
        return y >= MENUBAR_H and (y + h) <= (screen_h - DOCK_H)

    nf = _norm(first)
    fws = [w for w in words if _norm(w[0]) == nf and _in_content(w[2], w[4])]
    if not fws:
        return None
    if last is None:
        _, x, y, w, h = fws[0]
        return (x + w // 2, y + h // 2)
    nl = _norm(last)
    lws = [w for w in words if _norm(w[0]) == nl and _in_content(w[2], w[4])]
    for fw in fws:
        for lw in lws:
            if abs(fw[2] - lw[2]) <= y_tol and lw[1] >= fw[1]:
                return (
                    (fw[1] + lw[1] + lw[3]) // 2,
                    (fw[2] + lw[2] + lw[4]) // 2,
                )
    return None


def screen_text(ppm: str | None = None) -> str:
    """Flattened OCR text (all variants, lowercased). For keyword checks."""
    p = ppm or _screendump()
    words = ocr_words(p)
    return " ".join(w[0] for w in words).lower()


# ---------------------------------------------------------------------------
# Mouse click (QMP usb-tablet absolute coords)
# ---------------------------------------------------------------------------

def _qmp_raw(obj: dict) -> None:
    """One-shot QMP command outside the usual ``qmp.qmp()`` helper."""
    import json, socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    s.connect(QMP_SOCK)
    try:
        f = s.makefile("rwb", buffering=0)
        f.readline()  # greeting
        f.write(b'{"execute":"qmp_capabilities"}\n'); f.readline()
        f.write((json.dumps(obj) + "\n").encode()); f.readline()
    finally:
        s.close()


def click_pixel(x: int, y: int, screen_w: int, screen_h: int) -> None:
    ax = int(x * 32767 / screen_w)
    ay = int(y * 32767 / screen_h)
    _qmp_raw({"execute": "input-send-event", "arguments": {"events": [
        {"type": "abs", "data": {"axis": "x", "value": ax}},
        {"type": "abs", "data": {"axis": "y", "value": ay}},
    ]}})
    time.sleep(0.1)
    _qmp_raw({"execute": "input-send-event", "arguments": {"events": [
        {"type": "btn", "data": {"button": "left", "down": True}},
    ]}})
    time.sleep(0.08)
    _qmp_raw({"execute": "input-send-event", "arguments": {"events": [
        {"type": "btn", "data": {"button": "left", "down": False}},
    ]}})


def click_text(
    first: str,
    last: str | None = None,
    tries: int = 3,
    settle_s: float = 1.5,
    include_menubar: bool = False,
) -> bool:
    """Click a UI label identified by OCR. Retries on transient OCR misses.

    Set ``include_menubar=True`` to allow hitting targets inside the menu bar
    band (needed for menu bar items like "Utilities" in macOS Recovery).
    """
    for i in range(tries):
        p = _screendump()
        sw, sh = _screen_size(p)
        words = ocr_words(p)
        hit = find_phrase(
            words, first, last,
            screen_h=sh,
            exclude_chrome=not include_menubar,
        )
        if hit:
            cx, cy = hit
            click_pixel(cx, cy, sw, sh)
            time.sleep(settle_s)
            return True
        time.sleep(1.0)
    emit("warning", "vm",
         f"click_text missed {first!r}/{last!r} after {tries} tries")
    return False


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------

def wait_for_text(keywords: tuple[str, ...], deadline_s: int = 30, poll_s: float = 2.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        text = screen_text()
        if any(kw in text for kw in keywords):
            return True
        time.sleep(poll_s)
    return False
