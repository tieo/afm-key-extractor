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
    SA_COUNTRY = "sa_country"
    SA_LANGUAGES = "sa_languages"
    SA_ACCESSIBILITY = "sa_accessibility"
    SA_DATA_PRIVACY = "sa_data_privacy"
    SA_MIGRATION = "sa_migration"
    SA_APPLE_ID = "sa_apple_id"
    SA_TERMS = "sa_terms"
    SA_CREATE_ACCOUNT = "sa_create_account"
    SA_APPLE_ID_2 = "sa_apple_id_2"
    SA_TERMS_2 = "sa_terms_2"
    SA_LOCATION = "sa_location"
    SA_TIMEZONE = "sa_timezone"
    SA_ANALYTICS = "sa_analytics"
    SA_SCREEN_TIME = "sa_screen_time"
    SA_APPEARANCE = "sa_appearance"
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
    InstallState.WAITING_INSTALL: "Installing macOS (1–4 h on QEMU)",
    InstallState.BOOTING_INSTALLED: "Rebooting",
    InstallState.SA_COUNTRY: "Setup Assistant (1/15)",
    InstallState.SA_LANGUAGES: "Setup Assistant (2/15)",
    InstallState.SA_ACCESSIBILITY: "Setup Assistant (3/15)",
    InstallState.SA_DATA_PRIVACY: "Setup Assistant (4/15)",
    InstallState.SA_MIGRATION: "Setup Assistant (5/15)",
    InstallState.SA_APPLE_ID: "Setup Assistant (6/15)",
    InstallState.SA_TERMS: "Setup Assistant (7/15)",
    InstallState.SA_CREATE_ACCOUNT: "Setup Assistant (8/15)",
    InstallState.SA_APPLE_ID_2: "Setup Assistant (9/15)",
    InstallState.SA_TERMS_2: "Setup Assistant (10/15)",
    InstallState.SA_LOCATION: "Setup Assistant (11/15)",
    InstallState.SA_TIMEZONE: "Setup Assistant (12/15)",
    InstallState.SA_ANALYTICS: "Setup Assistant (13/15)",
    InstallState.SA_SCREEN_TIME: "Setup Assistant (14/15)",
    InstallState.SA_APPEARANCE: "Setup Assistant (15/15)",
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


# Per-state retry quotas.  Engine default is `DEFAULT_RETRY_BUDGET` (3); states
# listed here override it.  Set a higher number for handlers known to need
# multiple attempts to make progress (e.g. SA screen 8 dismisses a fresh error
# dialog and types fields again on each retry — up to ~12 attempts worst case).
DEFAULT_RETRY_BUDGET = 3

STATE_RETRY_BUDGET: dict[AnyState, int] = {
    InstallState.SA_CREATE_ACCOUNT: 12,  # error-dialog → Go Back → retype cycle
}


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
