# Wizard automation redesign

Status: design, not implemented. No code has been changed alongside this
document. The golden image (`mac_hdd_golden.img`) remains the primary
provisioning path; this document describes how the *fallback* path — what
`tracker.py::_run_setup_wizard` and `_create_account_from_recovery` do
today — should look when it is rewritten.

This is a speculative design. End-to-end testing against a fresh install
is explicitly out of scope; the chosen approach below was picked on
plausibility, not measured behavior. Every claim labelled **assumption**
below should be verified the next time someone is willing to wipe a
`mac_hdd_ng.img` and watch an installer run.

## macOS version scope

`tracker.py` sets `MACOS_VERSION = "ventura"` and `WIZARD_SCREENS` has
both `"catalina"` and `"ventura"` entries. The old `VM_SETUP.md` talks
about Ventura; the docstring of `_create_account_from_recovery` names
Catalina. The current golden image was baked by hand and could be either.

**Assumption.** This design targets Ventura (13.x). Every behavior
described as "Ventura" below is taken from code comments that reference
Ventura specifically (no "Not Now" on the transfer screen, shutdown
dialog defaults to "Shut Down", etc.). Catalina differs in the wizard
screen list but the Recovery-bypass argument is mostly version-independent
because it touches on-disk files, not UI. Where Ventura-specific facts
are stated without a code citation they are flagged `[assumption]`.

## 1. Facts carried forward from the old code

The items below are *facts* extracted from the existing implementation
and from `docs/VM_SETUP.md`. They are reused; no code is.

### QEMU control plane

- Monitor socket: `/tmp/airtag-vm-monitor.sock`. Used for `sendkey`,
  `screendump <path>`, `info status`.
- QMP socket: `/tmp/airtag-vm-qmp.sock`. Used for absolute-coordinate
  mouse input via `input-send-event` with `{type: abs, axis, value}` on
  a 0–32767 range, then `{type: btn, button: left, down}`.
- Framebuffer: **1280 × 800**. All on-screen coordinates are in this
  space. A common failure mode in the old code was assuming 1440 × 900
  (VM_SETUP.md mistake #1).
- VNC on host: `127.0.0.1:1` (display :1 → 5901). noVNC wraps it.
- PID file: `/tmp/airtag-vm-setup.pid`.

### Working QEMU key names (monitor `sendkey` dialect)

- Letters: `a`–`z`. Uppercase is `shift-<lower>`.
- Digits and punctuation have specific names: `spc`, `ret`, `minus`,
  `equal`, `dot`, `comma`, `slash`, `backslash`, `bracket_left`,
  `bracket_right`, `semicolon`, `apostrophe`, `grave_accent`.
- Arrows: `left`, `right`, `up`, `down`.
- Modifiers combined with `-`: `ctrl-f7`, `meta_l-f5` (Cmd+F5 — VoiceOver
  toggle), `ctrl-alt-right`, `ctrl-alt-spc`, `ctrl-alt-home`,
  `shift-tab`, `escape`.
- `tab`, `spc`, `esc` all work. `cmd-q` and friends are reached via
  `meta_l-<key>` (left Command).

### Screens observed and detection cues

From `_detect_screen` and `WIZARD_SCREENS`:

- **Desktop:** menu bar OCR contains "finder", "file  edit  view", or
  "go  window  help".
- **Login:** OCR contains "enter password", "log in", or "other users".
- **Boot picker:** dark frame, bright icon band at y ≈ 280–450; short
  label text or keywords `opencore / base system / macintosh`.
- **Recovery:** OCR keywords `macos recovery`, `reinstall macos`,
  `disk utility`, `restore from time machine`, `startup security utility`.
- **Terminal in Recovery:** OCR keywords `bash`, `-sh`, `root#`,
  `terminal`, `last login`, `diskutil`, `localhost`, `macintosh`.
- **UEFI error:** `failed to load`, `no bootable option`, `tianocore`,
  `pciroot`, `bdsdxe`.
- **Apple logo boot:** center bright, menu bar dark.

### Dialog defaults (load-bearing; wrong default = dead VM)

- "Are you sure you want to shut down?" after ACPI powerdown — default
  is **Shut Down**. Pressing Return confirms shutdown.
- After Cmd+Q on Migration Assistant — modal with **Shut Down** (default,
  Enter/Space) and **Restart**. Default shutdown kills the VM. The old
  code's mitigation is to press **Escape** to dismiss the dialog, never
  Enter/Space. This is confirmed-working.
- macOS Software License "I have read and agree" confirm — default is
  the **negative** button. Enter disagrees (VM_SETUP.md mistake #5).
- Agree / Skip confirm on Apple ID screens — default is typically the
  cancel/stay action, not proceed.

### Click offset observation

The "~20 px click offset" mentioned in the task description appears in
the code as `fallback_pos=(196, 670)` versus `fallback_pos=(986, 670)`,
versus targets like `(90, 670)` for "Not Now", `(260, 665)` for "Back",
and `(750, 455)` for small dialog buttons. The observation carried
forward: **on the 1280 × 800 framebuffer, primary wizard buttons sit at
y ≈ 665–670 and cluster near x = 196 (left/secondary), x = 986 (right/
primary), and x = 90 for corner links.** Any blind click should derive
from these anchors, not guessed coordinates.

### Screen order observations

- Ventura wizard order (from `WIZARD_SCREENS["ventura"]`): country →
  language → accessibility → privacy → (migration/transfer_info, which
  must be escaped without migrating) → apple_id → skip_confirm → terms
  → create_account → location → analytics → siri → screen_time →
  appearance → [done].
- After a successful migration the remaining screens are skipped;
  Catalina especially reboots straight to desktop (MEMORY:
  `project_catalina_wizard.md`). This is irrelevant to our flow because
  we *do not* want migration to proceed — migrating from the Recovery
  partition corrupts the boot (comment at tracker.py:1319).
- Keyboard Setup Assistant fires *after* the wizard and *before* ACPI
  shutdown is accepted; must be dismissed with Cmd+Q (VM_SETUP.md
  mistake #9).
- Password minimum length is 8 (VM_SETUP.md mistake #8; Setup Assistant
  silently rejects 6-char passwords with an undetected modal).

## 2. Root-cause hypothesis: why the Recovery/dscl bypass failed

Read `_create_account_from_recovery` at tracker.py:1967–2321 carefully.
It does six things, in order:

1. Kill the VM and restart QEMU **without** the `MacHDD` drive so
   OpenCore can only boot `BaseSystem.img` → Recovery.
2. Click menu-bar **Utilities → Terminal**.
3. `diskutil mount "Macintosh HD"` and `"Macintosh HD - Data"`; scan
   `/Volumes/*` for one containing
   `private/var/db/dslocal/nodes/Default/users`; remount read-write.
4. Hash the password with Python and write a user plist directly to
   `$DS/users/<user>.plist` (the comment at tracker.py:2142 explicitly
   says `dscl -f` is broken on Ventura with `eDSUnknownNodeName`).
5. `touch .AppleSetupDone`, plus `com.apple.SetupAssistant.plist` keys
   (`LastSeenBuddyBuildVersion=99Z99`, `DidSee*`), plus
   `AppleKeyboardUIMode=3` on `.GlobalPreferences`. Also copies the
   SetupAssistant plist into the user's home.
6. Kill the VM, restart with MacHDD attached, wait for desktop/login.

The comment at tracker.py:2316 captures the observed failure:

> `.AppleSetupDone didn't work — dscl path may have been wrong.`

And the fallback hypothesis at VM_SETUP.md mistake #11:

> `plistbuddy .AppleSetupDone` commands ran cleanly in Recovery, but
> Setup Assistant still fired on the next boot — root cause never
> identified.

### Things that are plausibly wrong in the old implementation

These are ranked by how likely each is the actual root cause, most
likely first.

**(a) The user plist is not a valid dsnode user record.** The old code
writes a minimal XML plist with keys `uid`, `gid`, `name`, `realname`,
`shell`, `home`, `generateduid`, and a cleartext `passwd`. Real users
in `/var/db/dslocal/nodes/Default/users/*.plist` on Ventura contain
additionally:

- `ShadowHashData` — an embedded binary plist containing
  `SALTED-SHA512-PBKDF2` parameters. This is the actual password
  verifier; `passwd` as a plaintext array is not read for login.
- `_writers_passwd`, `_writers_picture`, `_writers_realname`,
  `_writers_hint`, `_writers_unlockOptions` — each naming the user.
- `jpegphoto`, `picture`, `authentication_authority` pointing at
  `;ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2,…>`.
- `AppleMetaNodeLocation` `/Local/Default`.

Without `ShadowHashData` and `authentication_authority`, opendirectoryd
has no credential to verify. On the next boot macOS sees a user with
UID 501 in dslocal but cannot authenticate it, and Setup Assistant's
"did an admin account exist?" check fails — so it runs again to create
one. The SHA-512 hex the old code stores in `passwd` is not a format
any login path consults.

This is **the most likely root cause**. `.AppleSetupDone` gates Setup
Assistant on fresh boot, but on Ventura the check is more like "is
there at least one valid admin account?" — and ours isn't valid.

**(b) Permissions/ownership are wrong.** Writing a plist as root from
Recovery leaves it owned by root:wheel with whatever umask Recovery's
shell has. dslocal user plists are normally mode 600 owned by root,
which is fine — but if SIP or APFS snapshot integrity is in play the
write may not persist across reboot. On Ventura, `/` is a sealed system
volume; writes must go to `Macintosh HD - Data`. The code does scan
for the Data volume, but `$DS` is set to the first match; if it picks
the system volume's stub-dslocal instead, changes are discarded on
reboot when the sealed snapshot is remounted.

**(c) Data volume not mounted read-write.** `mount -uw "$D"` is called,
but if `$D` is the wrong path or if the APFS container is mounted
read-only due to integrity state, the write silently goes to an
overlay that doesn't survive reboot. On Ventura, Data volumes can be
in a state where `-uw` returns 0 but writes are actually buffered
nowhere useful [assumption — I don't have a cite for the
exact-error case, but the symptom matches].

**(d) SetupAssistant plist keys are wrong for Ventura.** The old code
writes `LastSeenBuddyBuildVersion=99Z99` and various `DidSee*` keys.
On Ventura these key names have changed at least once; the canonical
"skip" key set as of Ventura is reported to include
`DidSeeSyncSetup`, `DidSeeApplePaySetup`, `DidSeeTrueTonePrivacy`,
`SkipFirstLoginOptimization`, `GestureMovieSeen`, etc. [assumption —
no WebSearch available; names from memory]. Missing keys mean specific
panes still fire even if the top-level check is satisfied.

**(e) `$D` can be empty and the script keeps going.** The fallback loop
at tracker.py:2108 tries `diskutil mount` on a hardcoded list of disk
identifiers. If none of them are right, `$D=""`, and the heredoc still
writes to `/users/<user>.plist` at the filesystem root. The script's
`echo DS_OK` check is run in a Terminal session whose output is only
OCR'd — and the OCR string is truncated at 500 chars and not asserted
on. **The script cannot tell that its own writes failed.**

### Hypothesis

The primary reason `.AppleSetupDone` "did not work" is **(a)**: the
written user plist is not a functional account, so Setup Assistant
reruns on next boot to force account creation even though the sentinel
file exists. Secondary contributors are **(d)** (incomplete key set,
so individual panes reappear even if the top-level gate passes) and
**(e)** (silent failure when `$D` is misdetected).

### Does the hypothesis suggest a fix?

Yes. Two directions exist, and the design below takes the **simpler
one** because it sidesteps ShadowHashData generation entirely.

### Chosen approach

**Recovery-terminal path, but shaped around `sysadminctl` instead of
hand-rolled dslocal plists.** macOS ships
`/usr/sbin/sysadminctl -addUser <name> -password <pw> -admin` in
Recovery's shell environment. It is the documented way to create a
user; it writes all the fields in (a) correctly, including
`ShadowHashData` and `authentication_authority`. It targets the
currently-mounted macOS system, which in Recovery is addressed via
`-home`/`-fullName` flags and `dsconfigurelocal`-style arguments —
exactly the operation we want.

If `sysadminctl` in Recovery declines (possible — it needs the Local
node mounted in a way that opendirectoryd can see), fall back to
`dscl . -create /Users/<name>` after `launchctl load`-ing
`com.apple.opendirectoryd` against the mounted Data volume. Both of
these generate a correct `ShadowHashData`; our plist-wrangling does
not. [assumption — that `sysadminctl` is available and functional in
Ventura Recovery; the alternative is `dscl . -create` against a
chrooted opendirectoryd.]

GUI automation of the full Setup Assistant is rejected as primary
because:

- Migration Assistant on Ventura has no clean "Not Now" exit from the
  transfer screen (see tracker.py:1404 "Migration/transfer screen has
  no 'Not Now' button in Ventura"). The current code tries VoiceOver,
  Tab+Space, and menu-bar approaches and each is described as
  "non-deterministic".
- The shutdown dialog after Cmd+Q defaults to Shut Down. A wrong
  Enter/Space kills the VM, and Tab-to-Restart requires Full Keyboard
  Access which Setup Assistant doesn't have on by default.
- Overall: even a perfect OCR dispatcher is fighting Apple on a moving
  target. The Recovery bypass is a few hundred lines of on-disk writes
  that do not fight the UI at all.

GUI automation remains as **secondary fallback** for the case where
Recovery itself is unreachable (e.g. `BaseSystem.img` missing).

## 3. Design for the rewrite

### File layout

The existing `_run_setup_wizard` and `_create_account_from_recovery` in
`server/tracker.py` are deleted. Replaced by a new module
`server/wizard/` with:

- `wizard/__init__.py` — public entry point
  `bypass_setup_assistant()`, returns one of `{"golden", "recovery",
  "gui", "failed"}`.
- `wizard/qemu.py` — QEMU monitor + QMP + screenshot helpers (fresh
  implementation of the facts listed in §1; no code copied).
- `wizard/ocr.py` — PIL preprocessing and pytesseract wrapping;
  returns `(word, bbox)` tuples.
- `wizard/screens.py` — dispatch table keyed on recognized OCR
  headings, as described in VM_SETUP.md's "What a better Option 3
  looks like". Entries are plain dataclasses: `id`, `match_all`,
  `match_any`, `action`, `verify`.
- `wizard/recovery.py` — the primary path: boot into Recovery, create
  the user with `sysadminctl`, write the sentinel, reboot. See §4.
- `wizard/gui.py` — the fallback path: drive Setup Assistant
  screen-by-screen. See §5.

Import surface from `tracker.py` is one function:
`bypass_setup_assistant()`. Everything else is internal.

### Phase state

The existing `_set_phase` contract stays. New phase strings:

- `recovery_boot`, `recovery_terminal`, `recovery_create_user`,
  `recovery_reboot`.
- `gui_country`, `gui_migration`, …, `gui_account`, etc. —
  one per dispatch table entry.
- `verify_desktop`.

Phase transitions are single-writer from the wizard worker thread.

### Verification contract

Every action does three things, in order:

1. `before = screenshot()`; identify expected screen; abort if mismatch.
2. Act (click, type, keystroke).
3. `after = screenshot()`; assert a **positive** change — new screen
   identified, or the old screen's key text gone. Do not accept
   "setup_wizard" as a catch-all (VM_SETUP.md mistake #4).

If step 3 fails: up to two retries with coordinate jitter; then
escalate to keyboard fallback; then hard-error the phase. No silent
forward progress.

## 4. Recovery path — the primary bypass

### 4.1 Boot into Recovery

Boot Recovery by restarting QEMU **without** `MacHDD` attached, same
as today (tracker.py:1989). OpenCore then sees only OpenCore and
`BaseSystem.img` and auto-picks the latter after a short delay. A
secondary branch handles the 2-second boot-picker timeout by sending
`right` then `ret`.

Wait for the `recovery` screen via OCR. Timeout 180 s, poll 5 s.

**Fix relative to old code:** the old code hardcoded `_mouse_click(242,
10)` for the Utilities menu. The new code OCRs the menu bar for the
string "Utilities" and clicks its centroid. Same for the "Terminal"
dropdown item.

### 4.2 Find the Data volume reliably

The old code's `$D=""` silent failure (§2 cause (e)) is the first thing
to fix.

```
# In the Terminal — each line is typed and each result OCR'd and
# asserted before proceeding. Write a small shell preamble that
# prints a delimited report, then parse the OCR.

diskutil list -plist | plutil -convert xml1 -o - -   # skim for APFS data volume
diskutil apfs list                                    # grep for "Data"
# then mount *every* APFS volume; stop at the one whose
# /private/var/db/dslocal/nodes/Default/users dir exists.
```

Concretely, after mounting all candidates, the script emits sentinel
lines the OCR layer searches for:

- `WIZARD_SENTINEL DVOL=/Volumes/Macintosh HD - Data`
- `WIZARD_SENTINEL DVOL_RW=1` (after `mount -uw`)

If either sentinel is absent from the OCR of the terminal screenshot,
abort the phase with `"error: could not locate writable Data volume"`.
No plist writes unless both sentinels are present.

### 4.3 Create the user

Use `sysadminctl`, not a hand-rolled plist. `sysadminctl` in Recovery
operates against the mounted target:

```
sysadminctl \
  -addUser airtag \
  -fullName "airtag" \
  -password "airtagpw" \
  -admin \
  -home "/Volumes/Macintosh HD - Data/Users/airtag" \
  -shell /bin/zsh
```

Verify:

- Sentinel line after command: `WIZARD_SENTINEL USER_CREATED=$?`
  (zero = success).
- Second sentinel: `ls "$DVOL/private/var/db/dslocal/nodes/Default/
  users/airtag.plist" && echo WIZARD_SENTINEL USER_PLIST_OK` — the
  plist must exist after the command, and the file must be non-empty.
- Third sentinel: read back the plist and confirm it contains the
  string `ShadowHashData`; if not, the password is broken and login
  will fail, even though the plist exists. (This is the specific
  failure mode hypothesized in §2 cause (a).)

If `sysadminctl` returns non-zero, fall back to:

```
# Chroot-ish: load opendirectoryd pointing at the mounted Default node.
# Then dscl creates a proper user with ShadowHashData.
dscl -f "$DVOL/private/var/db/dslocal/nodes/Default" localonly \
  -create /Local/Default/Users/airtag
dscl -f ... -create /Local/Default/Users/airtag UniqueID 501
dscl -f ... -create /Local/Default/Users/airtag PrimaryGroupID 20
dscl -f ... -create /Local/Default/Users/airtag UserShell /bin/zsh
dscl -f ... -create /Local/Default/Users/airtag NFSHomeDirectory \
  /Users/airtag
dscl -f ... -passwd /Local/Default/Users/airtag airtagpw   # <-- this
                                                           # writes
                                                           # ShadowHashData
dscl -f ... -append /Local/Default/Groups/admin \
  GroupMembership airtag
```

The `eDSUnknownNodeName` error the old code mentions (comment at
tracker.py:2142) came from running `dscl -f <path> localonly -…`
against a path that wasn't the *Default* node. The path in the
fallback above explicitly targets `…/Default`. [assumption — this
fixes the error; the old comment doesn't say which path they tried.]

### 4.4 Sentinel files and SetupAssistant plist

After the user exists and has `ShadowHashData`:

- `touch "$DVOL/private/var/db/.AppleSetupDone"` (still required
  even with a valid user, per VM_SETUP.md mistake #11).
- Write `/Library/Preferences/com.apple.SetupAssistant.plist` with a
  complete Ventura-era `DidSee*` key set. The old code's set is
  incomplete (cause (d)). Canonical Ventura keys to set true
  [assumption, not verified on a running 13.x install]:
  `DidSeeCloudSetup`, `DidSeeSiriSetup`, `DidSeePrivacy`,
  `DidSeeAccessibility`, `DidSeeApplePaySetup`, `DidSeeSyncSetup`,
  `DidSeeTrueTonePrivacy`, `DidSeeAppearanceSetup`,
  `DidSeeScreenTime`, `GestureMovieSeen`, `SkipFirstLoginOptimization`
  — plus `LastSeenBuddyBuildVersion` set to a high per-major version
  tag.
- Copy the same plist into `/Users/airtag/Library/Preferences/` after
  `mkdir -p`.
- Set `AppleKeyboardUIMode=3` on `.GlobalPreferences` so Tab navigates
  between buttons in dialogs after login (this the old code got
  right, keep it).

Each write is followed by a `ls -la … && echo WIZARD_SENTINEL
<KEY>_OK` line; the OCR layer asserts each sentinel. **No silent
failures.**

### 4.5 Reboot and verify

Kill the Recovery VM (no MacHDD attached), restart QEMU with the full
drive list (MacHDD included), wait for one of `{desktop, login_screen}`.
Timeout 300 s.

**Fail-hard rule:** if the post-reboot screen is `setup_wizard`, the
Recovery bypass has failed. Do not fall through to GUI automation
automatically in production — log the failure, save a debug screenshot,
and set phase `error`. GUI automation is available behind an explicit
`?fallback=gui` query parameter on `/api/vm/start-setup` for operators
who want to try it. Rationale: the common case when Recovery fails is
that something on-disk is wrong (wrong Data volume, SIP surprise); GUI
automation will run into migration-assistant hell and make things
worse, not better.

## 5. GUI fallback — Setup Assistant driver

Only runs when explicitly requested. Structure:

- **Dispatch table**, not a loop with ad-hoc branches. Each entry:
  `id`, `match_all: list[str]`, `match_any: list[str]`, `action:
  Callable`, `verify: Callable -> bool`, `retry_strategies: list[
  Callable]`.
- No "setup_wizard" catch-all phase. If no dispatch entry matches, the
  phase errors out.
- Click resolution: always `find-button-by-OCR → click centroid`, never
  hardcoded pixels. Fallback is keyboard (`tab`/`spc`/`ret`), not
  coordinate jitter — coordinate jitter hides bugs.
- Dialog confirmations are **per-dialog** entries in the dispatch table.
  "Are you sure you want to shut down?" has its own entry with
  `action = send_key("esc")` because both visible buttons are dangerous
  (Shut Down default kills VM, Restart loses state).
- Form entry: before every field, `cmd+a` → `delete` → type. Password
  ≥ 8 chars, enforced in `VM_PASSWORD`.
- Keyboard Setup Assistant popup is in the dispatch table as its own
  screen with `action = cmd+q`.
- Wizard completes when the `desktop` or `login_screen` state is seen
  for two consecutive screenshots (prevents transient match during
  reboot).

### Ventura dispatch table (ordered)

Each entry has `match_all` (all must appear in full-screen OCR) and an
`action`. Coordinates are OCR-derived; the `(x, y)` shown is just the
fallback cluster the button is expected in.

| id | match_all | action | verify |
|---|---|---|---|
| `country` | `["country or region"]` | click "Continue" (~986, 670) | screen changes |
| `language` | `["written and spoken"]` | click "Continue" — never "Customize" | screen changes |
| `accessibility` | `["accessibility"]` | click "Not Now" (~196, 670) | screen changes |
| `data_privacy` | `["data", "privacy"]` | click "Continue" | screen changes |
| `migration_intro` | `["migration assistant"]` and not `"transfer information to this mac"` | click "Not Now" (~90, 670); on Ventura there *is* a "Not Now" link on the intro | screen changes AND does not become `transfer_info` |
| `transfer_info` | `["transfer information to this mac"]` | click "Back" (~260, 665); DO NOT Cmd+Q | returns to `migration_intro` |
| `shutdown_dialog` | `["want to shut"]` | `send_key("esc")` only; never tab/space/enter | dialog gone |
| `apple_id` | `["sign in with your apple id"]` | click "Set Up Later" | "are you sure you want to skip" appears |
| `skip_confirm` | `["skip"]` and overlay | click "Skip" (the non-default) | apple_id gone |
| `terms` | `["terms and conditions"]` | click "Agree", then in confirm dialog click "Agree" (non-default button on Ventura — confirm per-screenshot) | terms gone |
| `create_account` | `["create a computer account"]` | fill form (see below) | screen changes |
| `location` | `["enable location"]` | click "Continue" | screen changes |
| `analytics` | `["analytics"]` | click "Continue" | screen changes |
| `siri` | `["siri"]` | click "Continue" (or Not Now if present) | screen changes |
| `screen_time` | `["screen time"]` | click "Set Up Later" | screen changes |
| `appearance` | `["choose your look"]` | click "Continue" | desktop within 60 s |
| `keyboard_setup_popup` | `["identify your keyboard"]` | `cmd-q` | popup gone |

`create_account` form fill:

1. Full name field: Tab to it (FKA on), `cmd+a delete`, type "airtag".
2. Account name: auto-filled; verify via OCR, do not retype.
3. Password: `cmd+a delete`, type "airtagpw" (8 chars — VM_SETUP.md
   mistake #8).
4. Verify: same.
5. Hint: "hint".
6. Click "Continue".

The migration_intro handling is the **load-bearing** change from the
old GUI code. On Ventura there is a "Not Now" link in the lower-left
of the migration intro screen (confirmed by the old code's
`_mouse_click(90, 670)` at tracker.py:1650 and the dispatch table
entry at tracker.py:1412–1429). Click it directly. The old code's
spiral into VoiceOver + Tab+Space + menu-bar Terminal was reached
only because click detection on Migration Assistant buttons is
"unreliable in QEMU" (tracker.py:1582). Mitigation: **retry the click
with a 3-pixel spiral**, and if that fails escalate to Tab to Not Now
+ Space. Do not turn on VoiceOver — it breaks mouse clicks on the
next screen (tracker.py:2460 has a whole handler to turn it off).

## 6. What this design does not do

- Does not re-test the bypass. The user has accepted an unverified
  fallback. Next time `mac_hdd_golden.img` is absent and someone
  triggers `/api/vm/start-setup`, the Recovery path runs and either
  works, falls back to GUI automation behind the explicit flag, or
  fails hard with a debug screenshot in `/tmp/airtag-vm-wizard-*.png`.
- Does not alter the golden-image path. `vm_start_setup` still
  shortcuts to `shutil.copy2` when `mac_hdd_golden.img` exists.
- Does not change `bake-golden`.

## 7. Phased rollout

1. **Phase 1 — refactor (no behavior change).** Extract the QEMU
   monitor/QMP helpers and OCR helpers out of `tracker.py` into
   `wizard/qemu.py` and `wizard/ocr.py`. The existing
   `_create_account_from_recovery` and `_run_setup_wizard` are
   rewritten against the new helpers but preserve current behavior.
   Unit-testable against recorded screenshots.
2. **Phase 2 — Recovery rewrite.** Replace the hand-rolled plist write
   with `sysadminctl` + sentinel-asserted shell. This is the fix for
   the hypothesized root cause in §2. Ship behind the primary path.
3. **Phase 3 — GUI dispatch table.** Replace the loop in
   `_run_setup_wizard` with the explicit dispatch table. Move it
   behind `?fallback=gui` so it only runs on operator request.
4. **Phase 4 — delete dead code.** Remove VoiceOver-based migration
   handlers, `_escape_transfer_info`, and the stuck-strategy rotation.
   They exist because the migration step was unreliable; the dispatch
   table handles migration in one entry and either succeeds or fails
   cleanly.

## 8. Open questions

- Is `sysadminctl` actually available in Ventura's Recovery
  environment? I have not verified this; the code path assumes yes and
  falls back to `dscl -f …/Default` if not. Verify next time Recovery
  is booted interactively.
- Exact canonical set of `DidSee*` keys for Ventura — the list in §4.4
  is approximate. If any pane of Setup Assistant still fires despite
  a valid admin account, missing `DidSee*` is the likely cause.
- Does the Catalina VM in MEMORY reflect the current golden image, or
  is the golden image Ventura? The code says Ventura; the memory file
  documents Catalina. Settling this is important only if the golden
  image is ever rebuilt from scratch.
