"""State enums for both automation flows.

InstallState drives the one-time macOS installation that produces the
golden image.  RuntimeState drives every subsequent extraction run that
starts from the golden image.

Each value name doubles as its SSE/UI label — keep them human-readable.
"""

from __future__ import annotations

from enum import Enum
from typing import Union


class InstallState(Enum):
    IDLE = "idle"
    BOOTING_PICKER = "booting_picker"
    PICKER_SELECTING = "picker_selecting"
    WAITING_RECOVERY = "waiting_recovery"
    FORMAT_DISK = "format_disk"
    WAITING_FORMAT_DONE = "waiting_format_done"
    REINSTALL_CLICKING = "reinstall_clicking"
    WAITING_INSTALL = "waiting_install"
    BOOTING_INSTALLED = "booting_installed"
    SETUP_ASSISTANT = "setup_assistant"
    DISMISS_FIRST_BOOT = "dismiss_first_boot"
    SHUTTING_DOWN = "shutting_down"
    BAKING_GOLDEN = "baking_golden"
    DONE = "done"
    ERROR = "error"


class RuntimeState(Enum):
    IDLE = "idle"
    RESTORING_GOLDEN = "restoring_golden"
    BOOTING = "booting"
    PICKER_SELECTING = "picker_selecting"
    WAITING_LOGIN_SCREEN = "waiting_login_screen"
    LOGGING_IN = "logging_in"
    WAITING_DESKTOP = "waiting_desktop"
    DISABLING_SLEEP = "disabling_sleep"
    OPENING_APPLE_ID = "opening_apple_id"
    TYPING_CREDENTIALS = "typing_credentials"
    WAITING_2FA_OR_SIGNED_IN = "waiting_2fa_or_signed_in"
    AWAITING_2FA = "awaiting_2fa"
    TYPING_2FA = "typing_2fa"
    WAITING_SIGNED_IN = "waiting_signed_in"
    DISMISSING_POST_SIGNIN = "dismissing_post_signin"
    RESOLVING_APPLE_ID_UPDATE = "resolving_apple_id_update"
    ENABLING_FIND_MY = "enabling_find_my"
    WAITING_ICLOUD_SYNC = "waiting_icloud_sync"
    EXTRACTING_KEYS = "extracting_keys"
    SHUTTING_DOWN = "shutting_down"
    DONE = "done"
    ERROR = "error"


class FlowKind(Enum):
    INSTALL = "install"
    RUNTIME = "runtime"


# Human-readable labels shown in the UI progress bar.
INSTALL_STAGE_LABELS: dict[InstallState, str] = {
    InstallState.IDLE: "Idle",
    InstallState.BOOTING_PICKER: "Starting VM",
    InstallState.PICKER_SELECTING: "Selecting installer",
    InstallState.WAITING_RECOVERY: "Loading Recovery",
    InstallState.FORMAT_DISK: "Formatting disk",
    InstallState.WAITING_FORMAT_DONE: "Formatting disk",
    InstallState.REINSTALL_CLICKING: "Starting installer",
    InstallState.WAITING_INSTALL: "Installing macOS (20–45 min)",
    InstallState.BOOTING_INSTALLED: "Rebooting",
    InstallState.SETUP_ASSISTANT: "Running Setup Assistant",
    InstallState.DISMISS_FIRST_BOOT: "Finalising",
    InstallState.SHUTTING_DOWN: "Shutting down VM",
    InstallState.BAKING_GOLDEN: "Saving image",
    InstallState.DONE: "Installation complete",
    InstallState.ERROR: "Error",
}

RUNTIME_STAGE_LABELS: dict[RuntimeState, str] = {
    RuntimeState.IDLE: "Idle",
    RuntimeState.RESTORING_GOLDEN: "Restoring VM image",
    RuntimeState.BOOTING: "Starting VM",
    RuntimeState.PICKER_SELECTING: "Selecting macOS",
    RuntimeState.WAITING_LOGIN_SCREEN: "Waiting for login screen",
    RuntimeState.LOGGING_IN: "Logging in",
    RuntimeState.WAITING_DESKTOP: "Waiting for desktop",
    RuntimeState.DISABLING_SLEEP: "Configuring VM",
    RuntimeState.OPENING_APPLE_ID: "Opening Apple ID settings",
    RuntimeState.TYPING_CREDENTIALS: "Entering Apple ID",
    RuntimeState.WAITING_2FA_OR_SIGNED_IN: "Waiting for Apple response",
    RuntimeState.AWAITING_2FA: "Waiting for your 2FA code",
    RuntimeState.TYPING_2FA: "Submitting 2FA code",
    RuntimeState.WAITING_SIGNED_IN: "Completing sign-in",
    RuntimeState.DISMISSING_POST_SIGNIN: "Dismissing prompts",
    RuntimeState.RESOLVING_APPLE_ID_UPDATE: "Updating Apple ID",
    RuntimeState.ENABLING_FIND_MY: "Enabling Find My",
    RuntimeState.WAITING_ICLOUD_SYNC: "Waiting for iCloud sync",
    RuntimeState.EXTRACTING_KEYS: "Extracting AirTag keys",
    RuntimeState.SHUTTING_DOWN: "Shutting down VM",
    RuntimeState.DONE: "Keys extracted",
    RuntimeState.ERROR: "Error",
}

AnyState = Union["InstallState", "RuntimeState"]

# States where the popup watcher should skip its cycle.
WATCHER_SUPPRESSED_STATES: frozenset = frozenset({
    InstallState.WAITING_INSTALL,  # 30 min installer run, no GUI interaction
    InstallState.IDLE,
    InstallState.DONE,
    InstallState.ERROR,
    RuntimeState.AWAITING_2FA,    # blocked on human input — no QMP use
    RuntimeState.EXTRACTING_KEYS, # GUI Terminal session — avoid QMP races
    RuntimeState.IDLE,
    RuntimeState.DONE,
    RuntimeState.ERROR,
})
