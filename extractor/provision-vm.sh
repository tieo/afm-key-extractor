#!/usr/bin/env bash
# One-time macOS VM provisioning for AirTag key extraction.
# Downloads OSX-KVM, fetches macOS installer, creates disk image.
# After this, a one-time VNC session is needed to complete macOS setup.
set -euo pipefail

VM_DIR="${AIRTAG_VM_DIR:-/var/lib/airtag-tracker/osx-kvm}"
MACOS_VERSION="${MACOS_VERSION:-ventura}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

if [ -f "$VM_DIR/mac_hdd_ng.img" ]; then
  log "VM already provisioned at $VM_DIR"
  log "If you want to reprovision, remove $VM_DIR first."
  exit 0
fi

log "Provisioning macOS VM at $VM_DIR..."
mkdir -p "$VM_DIR"
cd "$VM_DIR"

# Clone OSX-KVM if not present
if [ ! -f "$VM_DIR/OpenCore/OpenCore.qcow2" ]; then
  log "Cloning OSX-KVM..."
  TMP_CLONE=$(mktemp -d)
  git clone --depth 1 https://github.com/kholia/OSX-KVM.git "$TMP_CLONE"
  # Move contents into VM_DIR
  cp -a "$TMP_CLONE"/. "$VM_DIR/"
  rm -rf "$TMP_CLONE"
  log "OSX-KVM cloned successfully"
fi

# Download macOS installer (fetch-macOS-v2.py saves to com.apple.recovery.boot/)
RECOVERY_DIR="$VM_DIR/com.apple.recovery.boot"
if [ ! -f "$RECOVERY_DIR/BaseSystem.dmg" ]; then
  log "Downloading macOS $MACOS_VERSION installer..."
  # Chunklist verification may fail on some images — download is still valid
  python3 "$VM_DIR/fetch-macOS-v2.py" --action download --board-id Mac-4B682C642B45593E --os latest || true
  if [ ! -f "$RECOVERY_DIR/BaseSystem.dmg" ]; then
    log "ERROR: BaseSystem.dmg download failed"
    exit 1
  fi
fi

# Convert to bootable image
if [ ! -f "$VM_DIR/BaseSystem.img" ]; then
  log "Converting BaseSystem.dmg..."
  qemu-img convert -O raw "$RECOVERY_DIR/BaseSystem.dmg" "$VM_DIR/BaseSystem.img" 2>/dev/null || \
    dmg2img "$RECOVERY_DIR/BaseSystem.dmg" "$VM_DIR/BaseSystem.img"
fi

# Create main disk (80GB is enough for macOS + Find My)
if [ ! -f "$VM_DIR/mac_hdd_ng.img" ]; then
  log "Creating 80GB disk image..."
  qemu-img create -f qcow2 "$VM_DIR/mac_hdd_ng.img" 80G
fi


log ""
log "=== VM Provisioned ==="
log "Open the AirTag Tracker web UI to complete macOS setup."
