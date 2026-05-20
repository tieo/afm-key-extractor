"""Single SSH/SCP entry point for the macOS guest.

All four older helpers (vm_ui.ssh, macos_adapter._ssh_run,
automation.runtime.extract._ssh, key_extraction._ssh) opened the same
sshpass+ssh subprocess with the same flags.  This module is now the
only place that builds the command line; everything else calls
``run`` / ``scp_from`` / ``scp_to``.
"""

from __future__ import annotations

import shutil
import subprocess as sp
from pathlib import Path

from . import vm_password
from .config import VM_SSH_HOST, VM_SSH_PORT, VM_USERNAME

_SSH_OPTS = (
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=5",
)


def find_sshpass() -> str:
    """Return path to sshpass binary, searching PATH then known Nix store dirs."""
    if found := shutil.which("sshpass"):
        return found
    for candidate in Path("/nix/store").glob("*-sshpass-*/bin/sshpass"):
        return str(candidate)
    return "sshpass"  # will fail with a clear FileNotFoundError if invoked


def _password(explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    return vm_password.get() or ""


def run(
    cmd: str,
    *,
    password: str | None = None,
    host: str = VM_SSH_HOST,
    port: int = VM_SSH_PORT,
    user: str = VM_USERNAME,
    timeout: int = 30,
) -> sp.CompletedProcess:
    """Execute *cmd* on the macOS guest over SSH and return the CompletedProcess.

    *password* defaults to the stored VM password.  Other parameters default to
    the configured guest endpoint (localhost:2222 by convention).
    """
    return sp.run(
        [
            find_sshpass(), "-p", _password(password),
            "ssh", *_SSH_OPTS,
            "-p", str(port),
            f"{user}@{host}",
            cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def scp_from(
    remote: str,
    local: Path | str,
    *,
    password: str | None = None,
    host: str = VM_SSH_HOST,
    port: int = VM_SSH_PORT,
    user: str = VM_USERNAME,
    timeout: int = 60,
    recursive: bool = True,
) -> sp.CompletedProcess:
    """Copy a file or directory from the guest back to the host."""
    args = [
        find_sshpass(), "-p", _password(password),
        "scp", *_SSH_OPTS,
        "-P", str(port),
    ]
    if recursive:
        args.append("-r")
    args += [f"{user}@{host}:{remote}", str(local)]
    return sp.run(args, capture_output=True, text=True, timeout=timeout)


def scp_to(
    local: Path | str,
    remote: str,
    *,
    password: str | None = None,
    host: str = VM_SSH_HOST,
    port: int = VM_SSH_PORT,
    user: str = VM_USERNAME,
    timeout: int = 60,
) -> sp.CompletedProcess:
    """Copy a host-side file to the guest."""
    return sp.run(
        [
            find_sshpass(), "-p", _password(password),
            "scp", *_SSH_OPTS,
            "-P", str(port),
            str(local), f"{user}@{host}:{remote}",
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def is_up(*, password: str | None = None, timeout: int = 8) -> bool:
    """Return True if SSH responds to a trivial command within *timeout*."""
    try:
        r = run("echo ready", password=password, timeout=timeout)
    except sp.TimeoutExpired:
        return False
    return r.returncode == 0 and "ready" in r.stdout
