#!/usr/bin/env bash
# Development startup script — runs the tracker server with all deps from nix-shell.
# Usage: bash dev-start.sh [--vm-dir PATH] [--data-dir PATH]
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
VM_DIR="${AIRTAG_VM_DIR:-$HOME/airtag-dev/osx-kvm}"
DATA_DIR="${AIRTAG_DATA_DIR:-$HOME/airtag-dev}"
VNC_PORT=5901
VNC_WS_PORT=6901
SERVER_PORT=8042

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --vm-dir) VM_DIR="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$DATA_DIR/keys" "$DATA_DIR/plists"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# --- noVNC WebSocket proxy (background) ---
NOVNC_PID=""
start_novnc() {
  log "Starting noVNC proxy (port $VNC_WS_PORT → VNC :$VNC_PORT)"
  websockify --web "$(nix-shell -p novnc --run "echo \${novnc}/share/webapps/novnc" 2>/dev/null || echo '/usr/share/novnc')" \
    "127.0.0.1:$VNC_WS_PORT" "127.0.0.1:$VNC_PORT" &>/dev/null &
  NOVNC_PID=$!
  log "noVNC PID: $NOVNC_PID"
}

cleanup() {
  log "Cleaning up…"
  [[ -n "$NOVNC_PID" ]] && kill "$NOVNC_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Check whether systemd airtag-novnc is available; if not, start manually.
if ! systemctl is-active --quiet airtag-novnc 2>/dev/null; then
  # Try to start it; if it fails (not a NixOS host), start websockify directly.
  if ! systemctl start airtag-novnc 2>/dev/null; then
    start_novnc
  fi
fi

log "Starting AirTag tracker server on port $SERVER_PORT"
log "VM dir:   $VM_DIR"
log "Data dir: $DATA_DIR"
log "UI:       http://localhost:$SERVER_PORT"

export AIRTAG_DATA_DIR="$DATA_DIR"
export AIRTAG_VM_DIR="$VM_DIR"
export AIRTAG_VM_ENABLED=true
export AIRTAG_VNC_WS_PORT="$VNC_WS_PORT"
export AIRTAG_PORT="$SERVER_PORT"

exec nix-shell \
  -p python313Packages.fastapi \
  -p python313Packages.uvicorn \
  -p python313Packages.pillow \
  -p python313Packages.cryptography \
  -p python313Packages.websockify \
  -p qemu \
  -p tesseract \
  -p sshpass \
  --run "export PYTHONPATH='$REPO/server':\$PYTHONPATH && python $REPO/server/tracker.py"
