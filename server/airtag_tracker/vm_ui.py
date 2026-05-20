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

from . import qmp, vm_ssh
from .config import QMP_SOCK
from .events import emit


# ---------------------------------------------------------------------------
# SSH (thin compatibility shim — real impl lives in vm_ssh)
# ---------------------------------------------------------------------------


def _find_tesseract() -> tuple[str, dict[str, str]]:
    """Return (tesseract_path, extra_env) searching PATH then Nix store.

    Sets TESSDATA_PREFIX to the sibling share/tessdata directory so that
    tesseract can find language data when run outside a nix-shell or
    system-level install.
    """
    if found := shutil.which("tesseract"):
        return found, {}
    # Search Nix store — hash-prefixed dirs like "abc123-tesseract-5.5.2".
    # Prefer the entry that ships eng.traineddata.
    for candidate in Path("/nix/store").glob("*-tesseract-*/bin/tesseract"):
        tessdata = candidate.parent.parent / "share" / "tessdata"
        if (tessdata / "eng.traineddata").exists():
            return str(candidate), {"TESSDATA_PREFIX": str(tessdata)}
    # Last resort — will fail with a clear error
    return "tesseract", {}


def ssh(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run *cmd* on the macOS guest.  Thin wrapper around vm_ssh.run for callers
    that already imported vm_ui."""
    return vm_ssh.run(cmd, timeout=timeout)


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
    # QEMU writes the PPM asynchronously; under heavy disk I/O (macOS installer)
    # the file can take several seconds to appear.  Poll up to 10s.
    for _ in range(9):
        if Path(path).exists():
            return path
        time.sleep(1.0)
    if not Path(path).exists():
        raise RuntimeError(f"QMP screendump produced no file at {path} after 10s")
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

    Runs all 5 tesseract variants in parallel (ThreadPoolExecutor) to keep
    total OCR time under 30 s even under QEMU CPU pressure.

    Returns ``(text, x, y, w, h)`` tuples in native VM coordinates."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        from PIL import Image, ImageOps
    except ImportError:
        emit("warning", "vm", "PIL unavailable — OCR disabled")
        return []

    with Image.open(ppm) as im:
        im = im.convert("RGB")
        im2x = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
        # Autocontrast on 2× helps with dark-background screens (OpenCore
        # picker, login window) where flat white-on-dark fools tesseract.
        im2x_ac = ImageOps.autocontrast(im2x)
        # Grayscale+autocontrast: removes color interference from wallpapers.
        im_gray2x = ImageOps.autocontrast(ImageOps.grayscale(im2x), cutoff=5).convert("RGB")
        variants = [
            (im,                      1),
            (ImageOps.invert(im),     1),
            (im2x_ac,                 2),
            (ImageOps.invert(im2x_ac), 2),
            (im_gray2x,               2),
        ]
        # Save all variant images while PIL objects are still open.
        tmps: list[tuple[str, int]] = []
        for vim, scale in variants:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tmp = tf.name
            vim.save(tmp)
            tmps.append((tmp, scale))

    tess_bin, tess_env = _find_tesseract()
    env = {**__import__("os").environ, **tess_env} if tess_env else None

    def _run_one(tmp: str, scale: int) -> list[tuple[str, int, int, int, int]]:
        try:
            r = subprocess.run(
                [tess_bin, tmp, "-", "tsv"],
                capture_output=True, text=True, timeout=30,
                env=env,
            )
            return _parse_tsv(r.stdout, scale)
        except Exception:
            return []
        finally:
            Path(tmp).unlink(missing_ok=True)

    words: list[tuple[str, int, int, int, int]] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_run_one, tmp, scale) for tmp, scale in tmps]
        for fut in as_completed(futures):
            words += fut.result()
    return words


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _prefix_extend(ocr_norm: str, target_norm: str) -> bool:
    """Match an OCR word that is the target with trailing characters stripped away
    by OCR hyphen-handling, e.g. "macintoshhd" when searching for "macintosh".

    Requires target ≥ 8 chars so short targets (like "agree", "install") never
    accidentally match longer words that start with them (like "disagree",
    "installation").  The containment guard `ocr.startswith(target)` means
    "disagree".startswith("agree") == False, so that never fires anyway.
    """
    return len(target_norm) >= 8 and ocr_norm.startswith(target_norm)


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
    fws = [w for w in words
           if (_norm(w[0]) == nf or _prefix_extend(_norm(w[0]), nf)) and _in_content(w[2], w[4])]
    if not fws:
        return None
    if last is None:
        _, x, y, w, h = fws[0]
        return (x + w // 2, y + h // 2)
    nl = _norm(last)
    lws = [w for w in words
           if (_norm(w[0]) == nl or _prefix_extend(_norm(w[0]), nl)) and _in_content(w[2], w[4])]
    for fw in fws:
        for lw in lws:
            if abs(fw[2] - lw[2]) <= y_tol and lw[1] >= fw[1]:
                return (
                    (fw[1] + lw[1] + lw[3]) // 2,
                    (fw[2] + lw[2] + lw[4]) // 2,
                )
    return None


def screen_text(ppm: str | None = None) -> str:
    """Flattened OCR text (all variants, lowercased). For keyword checks.

    If *ppm* is None a screendump is taken and deleted after use.
    If *ppm* is provided the caller owns the file — it is not deleted here.
    Returns empty string if the screendump fails (QEMU still initialising).
    """
    own = ppm is None
    try:
        p = _screendump() if own else ppm
    except Exception:
        return ""
    try:
        words = ocr_words(p)
        return " ".join(w[0] for w in words).lower()
    finally:
        if own:
            Path(p).unlink(missing_ok=True)


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


def scroll_down(clicks: int = 10, gap_s: float = 0.05) -> None:
    """Send scroll-wheel-down events at the current mouse position.

    Focus-independent: scrolls the element under the pointer regardless of
    which window or widget has keyboard focus.  Call immediately after a
    click that positioned the pointer inside the target scroll view."""
    for _ in range(clicks):
        _qmp_raw({"execute": "input-send-event", "arguments": {"events": [
            {"type": "btn", "data": {"button": "wheel-down", "down": True}},
        ]}})
        _qmp_raw({"execute": "input-send-event", "arguments": {"events": [
            {"type": "btn", "data": {"button": "wheel-down", "down": False}},
        ]}})
        time.sleep(gap_s)


def click_right_of(anchor: str, y_tol: int = 15, settle_s: float = 1.5) -> bool:
    """Click the element immediately to the right of *anchor* on the same line.

    Used when the target button OCRs unreliably but its left-neighbour is
    stable.  Example: the EULA / confirmation-sheet "Agree" button OCRs as
    "Ag&e" or "Agge", but "Disagree" always reads correctly.  Finding
    "Disagree" and clicking to its right always lands on "Agree" without
    any hardcoded coordinates.

    Returns False if *anchor* is not found or there is nothing to its right.
    """
    p = _screendump()
    try:
        sw, sh = _screen_size(p)
        words = ocr_words(p)
    finally:
        Path(p).unlink(missing_ok=True)
    na = _norm(anchor)
    anchors = [w for w in words if _norm(w[0]) == na]
    if not anchors:
        emit("warning", "vm", f"click_right_of: anchor {anchor!r} not found")
        return False
    _, rx, ry, rw, rh = anchors[0]
    candidates = [w for w in words if abs(w[2] - ry) <= y_tol and w[1] > rx + rw]
    if not candidates:
        emit("warning", "vm", f"click_right_of: nothing to the right of {anchor!r}")
        return False
    target = min(candidates, key=lambda w: w[1])
    _, tx, ty, tw, th = target
    click_pixel(tx + tw // 2, ty + th // 2, sw, sh)
    time.sleep(settle_s)
    return True


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
        p = None
        try:
            p = _screendump()
            sw, sh = _screen_size(p)
            words = ocr_words(p)
        except Exception:
            time.sleep(1.0)
            continue
        finally:
            if p is not None:
                Path(p).unlink(missing_ok=True)
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
        try:
            text = screen_text()
        except Exception:
            time.sleep(poll_s)
            continue
        if any(kw in text for kw in keywords):
            return True
        time.sleep(poll_s)
    return False
