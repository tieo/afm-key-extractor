"""Setup/provisioning status and macOS download endpoints.

Prefix: /api/setup
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter

from ...config import MACOS_VERSION, VM_DIR
from ...macos_adapter import get_adapter

router = APIRouter(prefix="/api/setup", tags=["setup"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ASSETS_DIR = Path("/app/assets")


def _adapter():
    return get_adapter(MACOS_VERSION)


def _ovmf_ready() -> bool:
    code = VM_DIR / "OVMF_CODE_4M.fd"
    vars_ = VM_DIR / "OVMF_VARS-1920x1080.qcow2"
    return code.exists() and vars_.exists()


def _opencore_ready() -> bool:
    return (VM_DIR / "OpenCore" / "OpenCore.qcow2").exists()


def _basesystem_ready(name: str) -> bool:
    return (VM_DIR / f"BaseSystem_{name}.img").exists()


def _golden_image_ready(name: str) -> bool:
    return _adapter().golden_image_path(VM_DIR).exists()


# ---------------------------------------------------------------------------
# Download state
# ---------------------------------------------------------------------------

_download_state: dict = {
    "running": False,
    "progress": "",
    "error": None,
}
_download_lock = threading.Lock()


def _run_download(shortname: str) -> None:
    """Background thread: fetch BaseSystem DMG then convert to raw."""
    global _download_state

    dest_img = VM_DIR / f"BaseSystem_{shortname}.img"

    with tempfile.TemporaryDirectory(prefix="airtag-basesystem-") as tmpdir:
        tmpdir_path = Path(tmpdir)

        # ---- fetch -------------------------------------------------------
        fetch_cmd = [
            "python3",
            str(_ASSETS_DIR / "fetch-macOS.py"),
            "--action", "download",
            "--shortname", shortname,
            "--outdir", tmpdir,
        ]
        with _download_lock:
            _download_state["progress"] = f"Downloading macOS {shortname} recovery from Apple..."

        try:
            proc = subprocess.Popen(
                fetch_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    with _download_lock:
                        _download_state["progress"] = line
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"fetch-macOS.py exited with code {proc.returncode}")
        except Exception as exc:
            with _download_lock:
                _download_state["running"] = False
                _download_state["error"] = str(exc)
            return

        # ---- find the downloaded DMG ------------------------------------
        dmg_files = list(tmpdir_path.glob("*.dmg"))
        if not dmg_files:
            with _download_lock:
                _download_state["running"] = False
                _download_state["error"] = (
                    f"fetch-macOS.py finished but no .dmg found in {tmpdir}"
                )
            return
        dmg_path = dmg_files[0]

        # ---- convert DMG → raw img --------------------------------------
        with _download_lock:
            _download_state["progress"] = f"Converting {dmg_path.name} → {dest_img.name} ..."

        VM_DIR.mkdir(parents=True, exist_ok=True)
        convert_cmd = [
            "qemu-img", "convert",
            "-f", "dmg",
            "-O", "raw",
            str(dmg_path),
            str(dest_img),
        ]
        try:
            proc = subprocess.Popen(
                convert_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    with _download_lock:
                        _download_state["progress"] = line
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"qemu-img convert exited with code {proc.returncode}")
        except Exception as exc:
            with _download_lock:
                _download_state["running"] = False
                _download_state["error"] = str(exc)
            return

    with _download_lock:
        _download_state["running"] = False
        _download_state["progress"] = f"{dest_img.name} ready"
        _download_state["error"] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
def get_status() -> dict:
    adapter = _adapter()
    name = adapter.name.lower()
    return {
        "ovmf_ready": _ovmf_ready(),
        "opencore_ready": _opencore_ready(),
        "basesystem_ready": _basesystem_ready(name),
        "golden_image_ready": _golden_image_ready(name),
        "macos_version": adapter.version,
        "macos_name": adapter.name,
    }


@router.post("/download-macos")
def download_macos() -> dict:
    global _download_state
    adapter = _adapter()
    name = adapter.name.lower()
    dest_img = VM_DIR / f"BaseSystem_{name}.img"

    if dest_img.exists():
        return {"status": "already_present"}

    with _download_lock:
        if _download_state["running"]:
            return {"status": "already_running"}
        _download_state = {
            "running": True,
            "progress": "Starting download...",
            "error": None,
        }

    t = threading.Thread(
        target=_run_download,
        args=(name,),
        daemon=True,
        name="basesystem-download",
    )
    t.start()
    return {"status": "started"}


@router.get("/download-macos/status")
def download_macos_status() -> dict:
    adapter = _adapter()
    name = adapter.name.lower()
    dest_img = VM_DIR / f"BaseSystem_{name}.img"

    with _download_lock:
        if not _download_state["running"] and not _download_state["progress"] and not _download_state["error"]:
            # No download has been attempted yet in this process lifetime.
            if dest_img.exists():
                return {
                    "running": False,
                    "progress": f"BaseSystem_{name}.img already present",
                    "error": None,
                }
        return dict(_download_state)
