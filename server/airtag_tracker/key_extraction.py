"""Extract AirTag decryption keys from the macOS VM.

Boots the VM if it isn't already running, SSHes in, unlocks the
keychain with the stored VM password, runs the decryptor to dump
``OwnedBeacons/*.plist`` into ``/tmp/airtag-export`` inside the VM,
copies the plists back, converts them to FindMy.py JSON, and (if we
started it) stops the VM. All of it over the already-forwarded port
2222 — no second QEMU.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess as sp
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import plist_conversion, qmp, vm, vm_password, vm_ui
from .config import DATA_DIR, PLISTS_DIR, VM_SSH_ENABLED_MARKER
from .events import emit

VM_USER = "airtag"
VM_HOST = "localhost"
VM_PORT = 2222

KEYS_DIR = DATA_DIR / "keys"


_lock = threading.Lock()
_running = False


def is_running() -> bool:
    with _lock:
        return _running


def start() -> dict:
    """Kick off an extraction in a background thread. No-op if one is
    already in progress."""
    global _running
    with _lock:
        if _running:
            return {"status": "already_running"}
        _running = True
    threading.Thread(target=_run, daemon=True, name="key-extraction").start()
    return {"status": "started"}


def _ssh(cmd: str, timeout: int = 60) -> sp.CompletedProcess:
    pw = vm_password.get() or ""
    return sp.run(
        [
            "sshpass", "-p", pw,
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


def _scp_to(local: Path, remote: str, timeout: int = 60) -> sp.CompletedProcess:
    pw = vm_password.get() or ""
    return sp.run(
        [
            "sshpass", "-p", pw,
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-P", str(VM_PORT),
            str(local), f"{VM_USER}@{VM_HOST}:{remote}",
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def _scp_from(remote: str, local: Path, timeout: int = 60) -> sp.CompletedProcess:
    pw = vm_password.get() or ""
    return sp.run(
        [
            "sshpass", "-p", pw,
            "scp", "-r",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-P", str(VM_PORT),
            f"{VM_USER}@{VM_HOST}:{remote}", str(local),
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def _decrypt(record_path: Path, key: bytes) -> dict:
    with record_path.open("rb") as f:
        enc = plistlib.load(f)
    if isinstance(enc, list) and len(enc) >= 3:
        nonce, tag, ct = enc[0], enc[1], enc[2]
    else:
        nonce = enc.get("Nonce") or enc.get("nonce")
        tag = enc.get("Tag") or enc.get("tag")
        ct = enc.get("Ciphertext") or enc.get("ciphertext")
    pt = AESGCM(key).decrypt(nonce, ct + tag, None)
    return plistlib.loads(pt)


def _ssh_up(timeout: int = 8) -> bool:
    r = _ssh("echo ready", timeout=timeout)
    return r.returncode == 0 and "ready" in r.stdout


def _enable_remote_login(password: str) -> None:
    """Drive Spotlight → Terminal → pipe password to `sudo -S launchctl
    load` via QMP keystrokes. Uses `sudo -S` (password via stdin) because
    typed input to sudo's tty prompt gets mangled by QMP send-key timing,
    but piping through `echo` works reliably. `launchctl load` avoids the
    Full Disk Access requirement that `systemsetup -setremotelogin` has."""
    emit("info", "extract", "SSH not up — enabling Remote Login via keystrokes")
    # Dismiss anything and break out of any stuck shell continuations.
    with qmp.qmp() as c:
        c.send_chord(["ctrl_l", "c"]); time.sleep(0.4)
        c.send_chord(["ctrl_l", "c"]); time.sleep(0.4)
        c.send_keys(["esc"]); time.sleep(0.5)
        c.send_chord(["meta_l", "spc"]); time.sleep(1.8)
        c.type_text("Terminal", gap_s=0.12); time.sleep(1.0)
        c.send_keys(["ret"]); time.sleep(7.0)  # Terminal first-launch is slow
        # Clear in case zsh got stuck in continuation mode earlier.
        c.send_chord(["ctrl_l", "c"]); time.sleep(0.3)
        c.type_text("clear", gap_s=0.12); c.send_keys(["ret"]); time.sleep(0.4)
        cmd = (
            f"echo {password} | sudo -S launchctl load -w "
            f"/System/Library/LaunchDaemons/ssh.plist 2>&1"
        )
        c.type_text(cmd, gap_s=0.12); c.send_keys(["ret"]); time.sleep(4.0)
        kick = (
            f"echo {password} | sudo -S launchctl kickstart -k "
            f"system/com.openssh.sshd 2>&1"
        )
        c.type_text(kick, gap_s=0.12); c.send_keys(["ret"]); time.sleep(3.0)
        c.send_chord(["meta_l", "q"])
    emit("info", "extract", "Remote Login keystroke sequence sent")


def _wait_ssh(deadline_s: int = 300) -> None:
    emit("info", "extract", f"Waiting for VM SSH (up to {deadline_s}s)")
    t0 = time.time()
    tried_enable = VM_SSH_ENABLED_MARKER.exists()
    while time.time() - t0 < deadline_s:
        if _ssh_up():
            if not VM_SSH_ENABLED_MARKER.exists():
                VM_SSH_ENABLED_MARKER.parent.mkdir(parents=True, exist_ok=True)
                VM_SSH_ENABLED_MARKER.write_text("1")
            emit("info", "extract", "VM SSH is up")
            return
        # If SSH hasn't come up 45s after login, assume Remote Login is
        # off and run the enable sequence (once per run).
        if not tried_enable and time.time() - t0 > 75:
            pw = vm_password.get() or ""
            if pw:
                try:
                    _enable_remote_login(pw)
                except Exception as e:
                    emit("warning", "extract", f"enable-ssh keystrokes failed: {e}")
            tried_enable = True
        time.sleep(3)
    raise RuntimeError("VM SSH never came up")


def _extract_beacon_key_via_terminal(pw: str) -> str:
    """Open Terminal in the VM and run `security find-generic-password` from
    there so the keychain ACL prompt appears in the GUI session. Type the
    keychain password and press Return (which triggers the default Allow
    button). Read the resulting hex key over SSH.

    SSH-direct `security` calls return errSecAuthFailed (-25308) because
    they have no UI session to present the ACL prompt — only Terminal
    works. Once granted in this session, repeat Terminal calls succeed
    without re-prompting."""
    pw_esc = pw.replace('"', '\\"').replace("$", "\\$")
    cmd = (
        f"clear; security find-generic-password -s BeaconStore "
        f"-a BeaconStoreKey -w > /tmp/beacon-key.hex 2>/tmp/beacon-key.err; "
        f"echo RC=$?"
    )

    # Spotlight → Terminal
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "spc"])
    time.sleep(1.5)
    qmp.type_text("Terminal", gap_s=0.10)
    time.sleep(0.6)
    qmp.send_keys(["ret"])
    time.sleep(6.0)  # cold launch

    qmp.type_text(cmd, gap_s=0.04)
    time.sleep(0.5)
    qmp.send_keys(["ret"])

    # Poll for SecurityAgent dialog OR for the file to be populated.
    # If access is cached for this session, no dialog appears.
    dialog_seen = False
    for i in range(24):
        time.sleep(0.5)
        try:
            qmp.screendump("/tmp/_keychain_dialog.ppm")
            time.sleep(0.2)
            txt = vm_ui.screen_text("/tmp/_keychain_dialog.ppm").lower()
        except Exception:
            txt = ""
        if "beaconstore" in txt and "always allow" in txt:
            dialog_seen = True
            emit("info", "extract", "Keychain ACL prompt visible — entering password")
            break
        # No dialog and key file populated → done
        check = _ssh("test -s /tmp/beacon-key.hex && echo READY", timeout=5)
        if "READY" in check.stdout:
            break

    if dialog_seen:
        qmp.type_text(pw, gap_s=0.06)
        time.sleep(0.4)
        qmp.send_keys(["ret"])  # Default button is Allow
        time.sleep(3.0)

    # Quit Terminal so it doesn't pile up
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "q"])
    time.sleep(0.5)

    r = _ssh("cat /tmp/beacon-key.hex 2>/dev/null", timeout=10)
    key_hex = r.stdout.strip()
    if not key_hex:
        err = _ssh("cat /tmp/beacon-key.err 2>/dev/null", timeout=5).stdout.strip()
        raise RuntimeError(
            f"Beacon key still empty after Terminal extraction: {err or '(no error)'}"
        )
    return key_hex


def _run() -> None:
    global _running
    we_started_vm = False
    try:
        if not vm.is_running():
            emit("info", "extract", "VM not running — booting it for extraction")
            vm.start()
            we_started_vm = True

        _wait_ssh()

        pw = vm_password.get() or ""
        if not pw:
            raise RuntimeError("VM password not available")

        emit("info", "extract", "Checking for AirTag records in VM")
        # First, just check whether OwnedBeacons exists and has records, and
        # if so tar them up. The keychain key is fetched separately via the
        # GUI Terminal trick (SSH security calls fail with -25308 because
        # the ACL prompt requires a UI session).
        cmd = (
            f"SPD=~/Library/com.apple.icloud.searchpartyd; "
            f"if [ ! -d \"$SPD/OwnedBeacons\" ] || "
            f"[ -z \"$(ls -A $SPD/OwnedBeacons 2>/dev/null)\" ]; then "
            f"  echo EMPTY; exit 0; "
            f"fi; "
            f"cd \"$SPD\" && "
            f"tar czf /tmp/airtag-records.tar.gz "
            f"OwnedBeacons $(test -d BeaconNamingRecord && echo BeaconNamingRecord) && "
            f"echo OK"
        )
        r = _ssh(cmd, timeout=60)
        if r.returncode != 0 or ("OK" not in r.stdout and "EMPTY" not in r.stdout):
            raise RuntimeError(
                f"VM tar failed (rc={r.returncode}): "
                f"{(r.stdout + r.stderr).strip()[:500]}"
            )
        if "EMPTY" in r.stdout:
            emit("info", "extract", "No AirTags paired in VM yet — nothing to extract")
            return

        emit("info", "extract", "Fetching BeaconStore key via GUI Terminal")
        key_hex = _extract_beacon_key_via_terminal(pw)
        # Stash in a file the rest of the flow expects.
        _ssh(f"echo {key_hex} > /tmp/beacon-key.hex", timeout=10)

        emit("info", "extract", "Copying records to server")
        with tempfile.TemporaryDirectory() as td:
            local = Path(td)
            r = _scp_from("/tmp/beacon-key.hex", local / "key.hex")
            if r.returncode != 0:
                raise RuntimeError(f"scp key failed: {r.stderr.strip()}")
            r = _scp_from("/tmp/airtag-records.tar.gz", local / "records.tar.gz")
            if r.returncode != 0:
                raise RuntimeError(f"scp records failed: {r.stderr.strip()}")

            key = bytes.fromhex((local / "key.hex").read_text().strip())
            with tarfile.open(local / "records.tar.gz") as tf:
                tf.extractall(local)
            owned = local / "OwnedBeacons"
            # Filter macOS AppleDouble metadata files (._*) that `tar` on
            # the VM sometimes emits for extended attributes.
            def _records(p):
                return [r for r in p.rglob("*.record") if not r.name.startswith("._")]
            if not owned.exists() or not _records(owned):
                emit("warning", "extract", "No beacon records — no AirTags paired?")
                return

            # Persist decrypted plists across runs so the OpenTagViewer
            # export endpoint can rebuild the zip without another extraction.
            plist_dir = PLISTS_DIR / "OwnedBeacons"
            naming_dir = PLISTS_DIR / "BeaconNamingRecord"
            for d in (plist_dir, naming_dir):
                if d.exists():
                    shutil.rmtree(d)
            plist_dir.mkdir(parents=True, exist_ok=True)
            for rec in _records(owned):
                try:
                    (plist_dir / f"{rec.stem}.plist").write_bytes(
                        plistlib.dumps(_decrypt(rec, key))
                    )
                except Exception as e:
                    emit("warning", "extract", f"decrypt {rec.stem}: {e}")

            named = local / "BeaconNamingRecord"
            if named.exists():
                naming_dir.mkdir(parents=True, exist_ok=True)
                # Apple nests naming records as <beacon-id>/<record-id>.record;
                # decrypt each and place under BeaconNamingRecord/<bid>/<rid>.plist
                # using the plist's own associatedBeacon/identifier fields.
                for rec in _records(named):
                    try:
                        pl = _decrypt(rec, key)
                    except Exception:
                        continue
                    bid = (pl.get("associatedBeacon") or rec.parent.name).upper()
                    rid = (pl.get("identifier") or rec.stem).upper()
                    (naming_dir / bid).mkdir(parents=True, exist_ok=True)
                    (naming_dir / bid / f"{rid}.plist").write_bytes(
                        plistlib.dumps(pl)
                    )

            KEYS_DIR.mkdir(parents=True, exist_ok=True)
            count = plist_conversion.convert_dir(
                plist_dir, KEYS_DIR,
                naming_dir=naming_dir if naming_dir.exists() else None,
            )
            emit("info", "extract", f"Extracted {count} AirTag key(s) → {KEYS_DIR}")

    except Exception as e:
        emit("error", "extract", f"Key extraction failed: {e}")
    finally:
        if we_started_vm:
            try:
                emit("info", "extract", "Stopping VM (we booted it for extraction)")
                vm.stop()
            except Exception as e:
                emit("warning", "extract", f"VM stop failed: {e}")
        with _lock:
            _running = False
