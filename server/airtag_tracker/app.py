"""Flask application factory."""

from __future__ import annotations

import logging

from flask import Flask

from . import account_storage, db, keys, polling, settings, systemd
from .config import DATA_DIR, KEYS_DIR, PORT, STATIC_DIR, VM_ENABLED
from .events import emit
from .routes import register as register_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def create_app() -> Flask:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except Exception:
        pass
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    db.init()

    app = Flask(__name__, static_folder=str(STATIC_DIR))
    register_routes(app)
    return app


def _log_startup_banner() -> None:
    s = settings.load()
    emit("info", "system", f"Server starting on port {PORT}")
    emit("info", "system", f"Data dir: {DATA_DIR}")
    emit("info", "system", f"VM enabled: {VM_ENABLED}")
    emit("info", "system", f"Account configured: {account_storage.exists()}")
    emit("info", "system", f"Loaded {keys.count()} AirTag key(s)")
    adaptive = "on" if s.get("adaptive", True) else "off"
    emit("info", "system",
         f"Polling: idle={s['idle_interval']}s, active={s['active_interval']}s, adaptive={adaptive}")


def run() -> None:
    app = create_app()
    _log_startup_banner()

    if VM_ENABLED and systemd.is_active("airtag-provision-vm"):
        emit("info", "vm", "VM provisioning is running, streaming logs")
        systemd.tail_journal_async("airtag-provision-vm", "vm")

    if VM_ENABLED:
        import threading
        from . import apple_creds, vm as vmmgr, vm_apple_signin
        def _autostart():
            try:
                st = vmmgr.status()
                if st.get("setup_complete") and not st.get("vm_running"):
                    emit("info", "vm", "Auto-starting VM on server startup")
                    vmmgr.start()
                if apple_creds.get():
                    emit("info", "vm", "Cached Apple creds found — auto-triggering sign-in")
                    try:
                        vm_apple_signin.start()
                    except Exception as e:
                        emit("warning", "vm", f"auto sign-in trigger failed: {e}")
            except Exception as e:
                emit("warning", "vm", f"VM autostart failed: {e}")
        threading.Thread(target=_autostart, daemon=True, name="vm-autostart").start()

    polling.start_background()
    app.run(host="127.0.0.1", port=PORT)
