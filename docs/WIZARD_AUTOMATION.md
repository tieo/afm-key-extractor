# Golden Image Build Automation

Goal: reproducible `mac_hdd_golden.img` built from the Apple-shipped
`BaseSystem.img` installer. No manual clicks at runtime.

## Approach

**D — Recovery-terminal bypass.** Install macOS normally from the
installer, then before Setup Assistant ever runs boot into Recovery,
mount the installed volume, plant `.AppleSetupDone` + an admin user
record via `sysadminctl`/`dscl`, reboot. Output = golden image.

Considered alternatives and rejected:

| # | Approach | Verdict |
|---|----------|---------|
| 1 | **D Recovery bypass** | chosen |
| 2 | F' Template-copy from existing golden | fallback if D's offline user-creation flakes |
| 3 | B QMP + template matching | fallback if no recovery route works |
| 4 | A QMP + OCR | previous attempt, too flaky |
| 5 | G HID replay | brittle (any timing drift breaks it) |
| 6 | E `startosinstall --installpackage` | requires signed pkg |
| - | J Just ship the golden | not reproducible from Apple media |

## Clean state (one-time on host)

```
/var/lib/airtag-tracker/osx-kvm/
  BaseSystem.img       # 3.0G, downloaded once from Apple, never refreshed
  mac_hdd_blank.img    # 194K qcow2, `qemu-img create -f qcow2 … 128G`
```

Reset per iteration: `cp --reflink=auto mac_hdd_blank.img mac_hdd_ng.img`.
Zero network, ~instant.

## VM invocation (installer mode)

Attach OpenCore + BaseSystem + blank MacHDD. Expose QMP + monitor sockets.

```
qemu-system-x86_64 -enable-kvm -m 8192 \
  -cpu Skylake-Client,… -machine q35 \
  -device qemu-xhci,id=xhci -device usb-kbd,bus=xhci.0 -device usb-tablet,bus=xhci.0 \
  -device isa-applesmc,osk=ourhardworkbythesewordsguardedpleasedontsteal\(c\)AppleComputerInc \
  -drive if=pflash,format=raw,readonly=on,file=OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file=OVMF_VARS-1920x1080.fd \
  -device ich9-ahci,id=sata \
  -drive id=OpenCoreBoot,if=none,snapshot=on,format=qcow2,file=OpenCore/OpenCore.qcow2 \
    -device ide-hd,bus=sata.2,drive=OpenCoreBoot \
  -drive id=MacHDD,if=none,file=mac_hdd_ng.img,format=qcow2 \
    -device ide-hd,bus=sata.4,drive=MacHDD \
  -drive id=InstallMedia,if=none,file=BaseSystem.img,format=raw \
    -device ide-hd,bus=sata.3,drive=InstallMedia \
  -netdev user,id=net0,hostfwd=tcp::2222-:22 \
    -device vmxnet3,netdev=net0,id=net0,mac=52:54:00:c9:18:27 \
  -device vmware-svga \
  -vnc 127.0.0.1:1 \
  -monitor unix:/tmp/airtag-vm-monitor.sock,server,nowait \
  -qmp unix:/tmp/airtag-vm-qmp.sock,server,nowait \
  -daemonize -pidfile /tmp/airtag-vm-setup.pid
```

Framebuffer is **1280×800**. All coordinates below are in that space.

## Driver primitives

- **Screendump**: `echo "screendump /tmp/shot.ppm" | socat - unix-connect:/tmp/airtag-vm-monitor.sock` → P6 PPM.
- **Key**: QMP `send-key` with qcode (`ret`, `right`, `spc`, `a`…`z`, `0`…`9`, modifiers).
- **Click**: QMP `input-send-event` with abs-axis events; map pixel `(x,y)` via `qx = x/1280*32767`, `qy = y/800*32767`; send `x` + `y` in a **single** event, then btn down + btn up.
- **Type text**: char-by-char `send-key`, shift-modified via `shift-<key>`.

## Per-step loop (discipline)

1. Reset: `cp mac_hdd_blank.img mac_hdd_ng.img`.
2. Start VM with installer media attached.
3. Run the automation written so far. It should reach the start of the
   next unautomated step.
4. Take over manually over noVNC / QMP; screenshot each sub-action;
   record the exact QMP commands.
5. Document the step below (append a new `### Step N` section).
6. Write code + tests for that step in `server/wizard/`.
7. Reset, run automation end-to-end, verify the new frontier.
8. Goto 4.

**Never skip ahead two steps.**

---

## Step 1 — OpenCore boot picker → boot installer

**Entry state**: QEMU booted with BaseSystem + blank MacHDD. OpenCore
boot picker shown after ~15 s.

**Screen** (`docs/wizard-screenshots/01-opencore-picker.png`):

- Two icons side by side, centered around y=390.
- Left: `EFI` at x≈564. Highlighted by default (selected).
- Right: `macOS Base System` at x≈715.
- Footer: power + back buttons at y=738.
- Pure-black background — easy to detect.

**Action**:

```
send-key right
send-key ret
```

No delay required between; OpenCore debounces.

**Exit state**: black screen → Apple logo with progress bar
(installer kernel loading) → **Recovery Utilities picker**
(`02-recovery-utilities.png`). BaseSystem boots directly into
Recovery; there is no separate "installer" UI — Recovery *is* the
installer front-end. Total time ~60–90 s.

**Verification**: screendump 90 s after `ret`. Expect dark grey
dialog centered on screen with four rows (Restore from TM, Reinstall
macOS Ventura, Safari, Disk Utility) and a "Continue" button bottom-
right. If still black, wait longer; if still boot picker, keys
didn't register — retry.

**Edge cases**:
- If installer media is missing, only `EFI` shows → `right` does nothing
  and we'd boot the empty MacHDD. Guard by checking `BaseSystem.img`
  exists before `qemu-system-x86_64`.
- OpenCore auto-boots the first entry (EFI) after its timeout.
  Automation must send `right` within that window. Long-term: set
  `Timeout=0` in OpenCore config for deterministic behaviour.

---

## Step 2 — Recovery Utilities → format blank disk (Disk Utility)

**Entry state**: Recovery Utilities picker shown
(`02-recovery-utilities.png`). Menu bar reads `Recovery File Edit
Utilities Window`. Four options listed; "Restore from Time Machine"
highlighted by default.

**Why Disk Utility first**: `mac_hdd_ng.img` is a 128 GiB raw qcow2
with no partition table. The Ventura installer will refuse to pick it
as a destination until it has an APFS container. Format it first,
name the volume `Macintosh HD`.

**Planned action (manual this iteration)**:

1. Click `Disk Utility` row (y≈175 approx, needs measurement).
2. Click `Continue`.
3. In Disk Utility, select the uninitialised 128 GiB disk.
4. `Erase` → Format `APFS`, Name `Macintosh HD`.
5. Quit Disk Utility → back to Recovery Utilities picker.

**Exit state**: Recovery Utilities picker, same layout, but the disk
is now mountable as `/Volumes/Macintosh HD` from a terminal and the
installer will accept it as a target in step 3.

**Code target**: a small state machine that clicks through Disk
Utility. Coordinates TBD — captured on the manual pass before
automation is written.

*(To be driven manually next; screenshots of each sub-action will
land in `docs/wizard-screenshots/02a-*`, `02b-*`, etc.)*

---

*(Later steps appended below as they are driven manually and
automated.)*
