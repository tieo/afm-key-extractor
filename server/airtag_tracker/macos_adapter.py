"""macOS version adapters.

One class per supported macOS version.  Each encapsulates the three things
that differ between releases:
- golden image filename (so Sonoma and Sequoia images coexist on disk)
- pre-reboot Recovery setup (Sequoia needs SIP partially disabled here)
- BeaconStore key extraction method (Sonoma = security CLI, Sequoia = Swift binary)

To add support for a new macOS version, subclass MacOSAdapter, implement the
three abstract methods, and add the class to _ADAPTERS.

Active version is selected by the AIRTAG_MACOS_VERSION env var (default: 14).
"""

from __future__ import annotations

import abc
import subprocess as sp
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import vm_ssh

if TYPE_CHECKING:
    from .automation.context import AutomationContext


class MacOSAdapter(abc.ABC):
    """Strategy for a specific macOS version.  Subclass for each new release."""

    # Subclasses set this to False until extract_beacon_key is fully wired up
    # end-to-end.  get_active_adapter() refuses to return an incomplete adapter
    # at startup so failures land at config-load instead of after a 1-hour
    # install + 30-min sign-in.
    is_implemented: bool = True

    @property
    @abc.abstractmethod
    def version(self) -> int:
        """Major macOS version number (14 = Sonoma, 15 = Sequoia, ...)."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable version name ('Sonoma', 'Sequoia', ...)."""

    @property
    def display_name(self) -> str:
        return f"{self.name} (macOS {self.version})"

    def golden_image_path(self, vm_dir: Path) -> Path:
        """Path to this version's golden disk image in vm_dir."""
        return vm_dir / f"mac_hdd_golden_{self.name.lower()}.img"

    def base_system_path(self, vm_dir: Path) -> Path:
        """Path to the macOS recovery installer image in vm_dir."""
        return vm_dir / f"BaseSystem_{self.name.lower()}.img"

    def pre_reboot_recovery_setup(self, ctx: AutomationContext) -> None:
        """Called from a Recovery Terminal session during install, before reboot.

        The terminal is open and focused when this is called.  Type any commands
        needed for OS-specific preparation.  Sonoma: no-op.
        """

    def focus_apple_id_email_field(self, ctx: AutomationContext) -> None:
        """Focus the email field in the Apple ID sign-in sheet via a direct pixel click.

        Keyboard navigation (Tab, Cmd+A) is unreliable here: System Settings
        opens with focus on the sidebar search field, and Tab from there moves
        between sidebar items, never reaching the email field in the content area.
        A pixel click at the known field position is deterministic.

        Coordinates assume a 1280×800 VM framebuffer with System Settings centered
        (the golden image bakes the window in this position).
        """
        from . import vm_ui as _vm_ui, qmp as _qmp
        # Click the email field then Cmd+A to select any pre-existing text so
        # the subsequent paste cleanly replaces it.
        _vm_ui.click_pixel(748, 445, 1280, 800)
        time.sleep(0.3)
        _qmp.send_chord(["meta_l", "a"])
        time.sleep(0.2)

    def navigate_to_find_my_mac(self, ctx: AutomationContext) -> None:
        """Navigate System Settings to the Find My Mac toggle and click Turn On.

        Default path (Sonoma 14 / Sequoia 15):
          Open iCloud section directly via URL → Show All → Find My Mac → Turn On

        We navigate to the iCloud section via x-apple.systempreferences URL instead
        of clicking the "iCloud" sidebar row.  The click approach is unreliable when
        the "Some iCloud Data Isn't Syncing" badge row also contains "iCloud" and gets
        clicked instead.

        Override if a future macOS version restructures the iCloud feature list.
        Raises RuntimeError if any navigation step fails.
        """
        from . import vm_ui as _vm_ui, qmp as _qmp
        from .events import emit as _emit

        # Navigate directly to the iCloud section within Apple ID settings.
        # Using the URL anchor skips the ambiguous sidebar-row click entirely.
        ICLOUD_URLS = (
            ("com.apple.systempreferences.AppleIDSettings", "iCloud"),
            ("com.apple.preferences.AppleIDPrefPane", "iCloud"),
        )
        # Keywords that confirm we're on the iCloud management page, not the sync-error page.
        ICLOUD_LANDED = ("icloud drive", "find my", "show more", "show all", "passwords", "icloud backup", "photos")

        def _open_icloud_section() -> bool:
            for bundle, anchor in ICLOUD_URLS:
                try:
                    _vm_ui.open_settings_pane(bundle, anchor, settle_s=4.0)
                except Exception:
                    continue
                # Give the page up to 15s to load — don't re-kill System Settings
                # if the first URL is working but just loading slowly.
                if _vm_ui.wait_for_text(ICLOUD_LANDED, deadline_s=15):
                    return True
            return False

        if not _open_icloud_section():
            _emit("warning", "macos_adapter",
                  "iCloud URL anchor failed — checking screen state")

        # Guard: if we're on the sync-error detail page, navigate back and retry.
        text = _vm_ui.screen_text()
        if "resume data sync" in text or "end-to-end encrypted" in text:
            _emit("info", "macos_adapter",
                  "Landed on iCloud sync error page — navigating back and retrying")
            if not _vm_ui.click_text("apple", "id", tries=2):
                with ctx.qmp_lock:
                    _qmp.send_keys(["esc"])
            time.sleep(2.0)
            if not _open_icloud_section():
                raise RuntimeError("Could not open iCloud section after sync-error recovery")
            text = _vm_ui.screen_text()
            if "resume data sync" in text or "end-to-end encrypted" in text:
                raise RuntimeError("Still on iCloud sync error page after retry")

        # macOS may show an "iCloud Drive" modal when first opening the iCloud
        # section — it asks whether to sync this Mac.  Dismiss it with Done
        # before looking for the Show All / Find My Mac rows.
        text = _vm_ui.screen_text()
        if "sync this mac" in text or ("icloud drive" in text and "done" in text):
            _emit("info", "macos_adapter", "iCloud Drive dialog — clicking Done")
            if not _vm_ui.click_text("Done", tries=2):
                with ctx.qmp_lock:
                    _qmp.send_keys(["ret"])
            time.sleep(1.5)

        # The button to expand the app list is "Show More Apps..." in Sonoma or
        # "Show All" in some configurations.  Try both.
        for show_label in (("Show", "More"), ("Show", "All")):
            if _vm_ui.click_text(*show_label, tries=2):
                break
        else:
            _emit("warning", "macos_adapter",
                  "Could not click 'Show More Apps' / 'Show All' — Find My row may still be visible")
        time.sleep(1.0)
        if not _vm_ui.click_text("Find", "Mac", tries=3):
            raise RuntimeError("Could not locate 'Find My Mac' row in iCloud features list")
        time.sleep(1.5)
        _vm_ui.click_text("Turn", "On", tries=2)
        time.sleep(1.0)

    @abc.abstractmethod
    def extract_beacon_key(self, *, vm_password: str) -> str:
        """Extract the BeaconStore AES key from the running VM.

        Returns the key as a lowercase hex string.
        Raises RuntimeError on any failure.
        """


class SonomaAdapter(MacOSAdapter):
    """macOS 14 Sonoma.

    Key extraction: ``security find-generic-password -s BeaconStore -a BeaconStoreKey -w``
    via a GUI Terminal session (required because the keychain ACL prompt needs a UI session).
    No SIP changes needed.
    """

    @property
    def version(self) -> int:
        return 14

    @property
    def name(self) -> str:
        return "Sonoma"

    def pre_reboot_recovery_setup(self, ctx: AutomationContext) -> None:
        pass

    def extract_beacon_key(self, *, vm_password: str) -> str:
        from . import qmp, vm_ui
        from .events import emit

        def ssh(cmd: str, timeout: int = 60) -> sp.CompletedProcess:
            return vm_ssh.run(cmd, password=vm_password, timeout=timeout)

        # Open Terminal via Spotlight → type the security command.
        with qmp.qmp() as c:
            c.send_chord(["meta_l", "spc"])
        time.sleep(1.5)
        qmp.type_text("Terminal", gap_s=0.10)
        time.sleep(0.6)
        qmp.send_keys(["ret"])
        time.sleep(6.0)  # cold launch

        extract_cmd = (
            "clear; security find-generic-password -s BeaconStore "
            "-a BeaconStoreKey -w > /tmp/beacon-key.hex 2>/tmp/beacon-key.err; "
            "echo RC=$?"
        )
        qmp.type_text(extract_cmd, gap_s=0.04)
        time.sleep(0.5)
        qmp.send_keys(["ret"])

        # Poll for SecurityAgent ACL dialog or direct key file population.
        # If keychain access was previously granted in this session, no dialog appears.
        dialog_seen = False
        dump_path = "/tmp/_sonoma_keychain_acl.ppm"
        for _ in range(24):
            time.sleep(0.5)
            try:
                qmp.screendump(dump_path)
                time.sleep(0.2)
                txt = vm_ui.screen_text(dump_path).lower()
            except Exception:
                txt = ""
            finally:
                Path(dump_path).unlink(missing_ok=True)

            if "beaconstore" in txt and "always allow" in txt:
                dialog_seen = True
                emit("info", "macos_adapter.sonoma", "Keychain ACL prompt — entering VM password")
                break
            check = ssh("test -s /tmp/beacon-key.hex && echo READY", timeout=5)
            if "READY" in check.stdout:
                break

        if dialog_seen:
            qmp.type_text(vm_password, gap_s=0.06)
            time.sleep(0.4)
            qmp.send_keys(["ret"])  # default button is Allow
            time.sleep(3.0)

        # Quit Terminal (keeps window stack tidy across runs).
        with qmp.qmp() as c:
            c.send_chord(["meta_l", "q"])
        time.sleep(0.5)

        r = ssh("cat /tmp/beacon-key.hex 2>/dev/null", timeout=10)
        key_hex = r.stdout.strip()
        if not key_hex:
            err = ssh("cat /tmp/beacon-key.err 2>/dev/null", timeout=5).stdout.strip()
            raise RuntimeError(
                f"Sonoma beacon key empty after Terminal extraction. "
                f"security error: {err or '(none)'}"
            )
        return key_hex


class SequoiaAdapter(MacOSAdapter):
    """macOS 15 Sequoia.  WORK IN PROGRESS — see memory/project_sequoia_wip.md.

    Key extraction requires:
    1. SIP partially disabled during install Recovery (csrutil enable --without nvram)
    2. amfi_get_out_of_my_way=1 nvram boot-arg set before first user boot
    3. beaconstorekey-extractor Swift binary compiled and baked into the golden image

    pre_reboot_recovery_setup() handles step 1 (types into the open Recovery Terminal).
    Steps 2 and 3 are not yet automated — this adapter raises NotImplementedError on
    extract_beacon_key() until the full plan is implemented.
    """

    is_implemented = False  # gate at get_active_adapter() instead of failing late

    @property
    def version(self) -> int:
        return 15

    @property
    def name(self) -> str:
        return "Sequoia"

    def pre_reboot_recovery_setup(self, ctx: AutomationContext) -> None:
        from . import qmp
        from .events import emit
        # Terminal is already open from format_disk.wait_done.
        # Partially disable SIP so beaconstorekey-extractor can hold the
        # com.apple.icloud.searchpartyuseragent keychain-access-group entitlement.
        emit("info", "macos_adapter.sequoia",
             "Partially disabling SIP (csrutil enable --without nvram)")
        qmp.type_text("csrutil enable --without nvram", gap_s=0.05)
        qmp.send_keys(["ret"])
        time.sleep(3.0)
        emit("info", "macos_adapter.sequoia", "SIP partially disabled")

    def extract_beacon_key(self, *, vm_password: str) -> str:
        raise NotImplementedError(
            "Sequoia key extraction is not yet implemented. "
            "Resume plan: memory/project_sequoia_wip.md. "
            "Requires beaconstorekey-extractor binary baked into golden image."
        )


_ADAPTERS: dict[int, type[MacOSAdapter]] = {
    14: SonomaAdapter,
    15: SequoiaAdapter,
}

SUPPORTED_VERSIONS: list[int] = sorted(_ADAPTERS.keys())


def get_adapter(version: int) -> MacOSAdapter:
    """Return a fresh adapter instance for the given macOS major version."""
    if version not in _ADAPTERS:
        raise ValueError(
            f"Unsupported macOS version {version}. "
            f"Supported: {SUPPORTED_VERSIONS}"
        )
    return _ADAPTERS[version]()


def get_active_adapter() -> MacOSAdapter:
    """Return the adapter for the version in AIRTAG_MACOS_VERSION (default: 14).

    Refuses to return adapters where ``is_implemented`` is False so flow startup
    fails fast rather than hours later at the extraction step.
    """
    from .config import MACOS_VERSION
    adapter = get_adapter(MACOS_VERSION)
    if not adapter.is_implemented:
        raise RuntimeError(
            f"AIRTAG_MACOS_VERSION={MACOS_VERSION} ({adapter.display_name}) is not "
            "yet fully supported — key extraction is unimplemented. "
            "Use AIRTAG_MACOS_VERSION=14 (Sonoma) or finish "
            "memory/project_sequoia_wip.md before retrying."
        )
    return adapter
