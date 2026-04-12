# macOS VM Setup

The AirTag key extractor runs a macOS Ventura VM under QEMU/KVM. It needs a
Mac user account to exist before `iCloudKeychainService` can be queried.

There are two paths for getting that account in place. Option 1 is primary;
Option 3 is a fallback / reproducibility option that we haven't gotten to
work reliably yet.

---

## Option 1 — Golden image (primary)

Complete the Setup Assistant **once** through noVNC, snapshot the disk, and
restore the snapshot on every future boot. No wizard automation at runtime.

### State location (not in git)

The golden image lives on the host, not in the repo:

```
/var/lib/airtag-tracker/osx-kvm/mac_hdd_golden.img    # ~28 GB
```

It's treated as local host state like `mac_hdd_ng.img`, sops-decrypted
secrets, and the LUKS-wrapped restic repo. Back it up with whatever backs
up the rest of `/var/lib`. It is **not** committed to git — binary qcow2
files defeat diffing and blow up clone size.

### How `start-setup` uses it

`POST /api/vm/start-setup` (see `server/tracker.py::vm_start_setup`):

1. If `mac_hdd_golden.img` exists → `shutil.copy2` it over `mac_hdd_ng.img`,
   skip the `BaseSystem.img` installer media, skip the `auto_install_worker`
   thread. Phase jumps straight to `"done"`.
2. If it doesn't exist → fall back to the installer + wizard-automation path
   (Option 3, still flaky).

OpenCore still prompts for which disk to boot on cold start. The VM picks
the wrong default (EFI), so after `start-setup` the caller must send
`right` then `ret` via the QMP socket to select `Macintosh HD`. This can
be wired into `vm_start_setup` later if it becomes annoying.

### Baking the golden image

One-time procedure. Everything runs on the host.

```bash
# 1. Start the VM with no automation
curl -fsS -X POST http://127.0.0.1:8042/api/vm/start-manual

# 2. Connect via noVNC (port 6901) and complete the Setup Assistant by hand.
#    - Country: anything
#    - Migration: "Not Now"
#    - Apple ID: "Set Up Later"
#    - Terms: Agree
#    - Account: name=tracker, password=airtagpw (>=8 chars), hint=hint
#    - Location/Analytics/Screen Time/Appearance: defaults
#    - Quit the "Keyboard Setup Assistant" popup (Cmd+Q)
#    - Apple menu -> Shut Down -> confirm

# 3. Wait for qemu to exit, then snapshot:
curl -fsS -X POST http://127.0.0.1:8042/api/vm/bake-golden
```

The `bake-golden` endpoint backs up any previous golden to
`mac_hdd_golden.img.bak` before overwriting, so running it twice is safe.

### Credentials

Stored at `/var/lib/airtag-tracker/data/vm-password` (not in git). Default
from the procedure above: `airtagpw`.

### When to re-bake

- macOS version upgrade (for a fresh feature set).
- VM was corrupted and restored from a bad snapshot.
- You want the VM to forget a specific keychain entry and start clean.

---

## Option 3 — Scripted wizard (fallback, fragile)

Drive the Setup Assistant entirely from code. We spent a long time trying
to make this work before giving up and going with Option 1. The code is
still present as a fallback in `tracker.py` (`_run_setup_wizard` and
friends), activated when no golden image is present.

### Why it failed the first time — mistakes made

1. **Wrong resolution.** Helper assumed 1440×900 while the actual QEMU
   framebuffer is 1280×800. Every absolute-coordinate click was skewed
   by ~15%. We kept "fixing" by nudging click coords instead of fixing
   the resolution.
2. **Coordinate estimation from thumbnails.** "Not Now" was estimated at
   `x=155` from a shrunken screenshot; the actual position was `x=310`.
   Should have cropped-then-read every button before clicking.
3. **Click-fire-and-forget.** After sending a click we assumed it worked
   and moved on. If QEMU dropped the event or the coordinate was off,
   automation charged ahead into the wrong screen and got stuck.
4. **Heuristic screen detection.** `_detect_screen` returned
   `"setup_wizard"` as a catch-all fallback, and the completion check
   accepted that as "done". It reported success on screens it didn't
   recognise.
5. **Return-key assumptions.** Some confirm dialogs (e.g. "I have read
   and agree to the macOS Software License Agreement") default to the
   *negative* button — pressing Return Disagrees. Always verify which
   button is default before using keyboard shortcuts.
6. **VoiceOver-driven navigation was non-deterministic.** Migration
   Assistant's VoiceOver start-focus varies per boot, so "VO Right ×3
   then activate" sometimes hit "Not Now" and sometimes hit a radio
   button.
7. **Full Keyboard Access assumption.** We leaned on Tab-to-navigate in
   places where FKA wasn't on by default; Tab did nothing.
8. **Password length.** Initial automation set a 6-char password; Setup
   Assistant silently rejected it with a modal we didn't detect. Use
   ≥8 chars.
9. **Keyboard Setup Assistant blocks shutdown.** After wizard completion
   macOS pops "identify your keyboard" over the desktop and blocks ACPI
   shutdown until it's dismissed (Cmd+Q quits it). Our code hit ACPI
   directly and the VM hung.
10. **ACPI powerdown needs user confirmation.** macOS shows "Are you sure
    you want to shut down?" in response to ACPI. Either press Return to
    confirm, or use Apple menu → Shut Down for a reliable flow.
11. **Recovery-terminal bypass didn't take.** `plistbuddy .AppleSetupDone`
    commands ran cleanly in Recovery, but Setup Assistant still fired on
    the next boot — root cause never identified.
12. **No per-step verification.** Every step should re-screenshot and
    assert it's on the *next* expected screen before proceeding. We
    assumed forward progress.

### What a better Option 3 looks like

Based on driving the wizard by hand once:

- **Resolution-aware coordinates.** Query actual framebuffer size via
  QMP (`query-display-options` or screendump dimensions) and derive all
  click targets proportionally.
- **Find-then-click.** For every button, crop a region, OCR / template-
  match the label, click its centroid. Never hardcode pixels.
- **Verify every action.** Screenshot before and after; diff or re-OCR
  to confirm the screen changed. If not, retry with coord jitter, then
  escalate to keyboard fallback (Tab/Return), then fail loudly.
- **Catalogue of dialogs, not screens.** Build a dispatch table keyed on
  recognised headings ("Migration Assistant", "Sign In with Your Apple
  ID", "Terms and Conditions", "Create a Computer Account", …) with an
  explicit handler for each. Any unrecognised heading is a hard stop,
  not a "setup_wizard" catch-all.
- **Explicit confirm-dialog handling.** "Are you sure you want to skip",
  "I have read and agree", "You haven't provided all the required
  information", "Are you sure you want to shut down" — each has a
  default that may or may not be what we want. Encode per-dialog.
- **Form input.** Use `cmd+a` → `delete` → type before every field to
  avoid appending to pre-filled content. Password ≥8 chars.
- **Clean shutdown.** Apple menu → Shut Down → Return to confirm.
  Dismiss Keyboard Setup Assistant (Cmd+Q) first if present.
- **Vision-in-the-loop for development.** Drive the wizard interactively
  from a screenshot + inspect loop to *derive* the dispatch table, then
  freeze the coords/templates for runtime. The existing code tried to
  do this blind and that's fundamentally the reason it kept drifting.

### Retesting

Not currently retested against a real install — doing so means deleting
the working `mac_hdd_golden.img` (or installing in parallel) and
sitting through the multi-hour Ventura install again. The improved
handlers live in `tracker.py` and will be exercised the next time
someone provisions a fresh host.
