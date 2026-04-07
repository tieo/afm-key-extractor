#!/usr/bin/env bash
# One-time macOS VM provisioning for AirTag key extraction.
# Downloads a pre-built Catalina image (SSH enabled, user/alpine) and OSX-KVM boot files.
# After this, only Apple ID sign-in via VNC is needed.
set -euo pipefail

VM_DIR="${AIRTAG_VM_DIR:-/var/lib/airtag-tracker/osx-kvm}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

if [ -f "$VM_DIR/mac_hdd_ng.img" ]; then
  log "VM already provisioned at $VM_DIR"
  exit 0
fi

log "Provisioning macOS VM at $VM_DIR..."
mkdir -p "$VM_DIR"
cd "$VM_DIR"

# Clone OSX-KVM for OpenCore + OVMF firmware files
if [ ! -f "$VM_DIR/OpenCore/OpenCore.qcow2" ]; then
  log "Cloning OSX-KVM (boot files)..."
  TMP_CLONE=$(mktemp -d)
  git clone --depth 1 https://github.com/kholia/OSX-KVM.git "$TMP_CLONE"
  cp -a "$TMP_CLONE"/. "$VM_DIR/"
  rm -rf "$TMP_CLONE"
  log "OSX-KVM cloned successfully"
fi

# Download pre-built Catalina image (~20GB, has SSH + user:alpine)
if [ ! -f "$VM_DIR/mac_hdd_ng.img" ]; then
  log "Downloading pre-built macOS Catalina image (~20GB)..."
  curl -L -o "$VM_DIR/mac_hdd_ng.img" "https://images2.sick.codes/mac_hdd_ng_auto.img"
  log "Download complete"
fi

# Build OpenCore with NOPICKER (auto-boots without boot menu)
if [ -f "$VM_DIR/OpenCore/OpenCore.qcow2" ] && [ -f "$VM_DIR/custom/opencore-image-ng.sh" ]; then
  log "Configuring OpenCore NOPICKER..."
  PLIST_URL="https://raw.githubusercontent.com/sickcodes/Docker-OSX/master/custom/config-nopicker-custom.plist"
  curl -L -o "$VM_DIR/config-nopicker.plist" "$PLIST_URL"
  cd "$VM_DIR"
  if [ -x "$VM_DIR/custom/opencore-image-ng.sh" ]; then
    "$VM_DIR/custom/opencore-image-ng.sh" \
      --cfg "$VM_DIR/config-nopicker.plist" \
      --img "$VM_DIR/OpenCore/OpenCore.qcow2" 2>/dev/null || true
  fi
  log "OpenCore NOPICKER configured"
fi

log ""
log "=== VM Provisioned ==="
log "Pre-built macOS Catalina with SSH enabled (user: user, password: alpine)"
log "Open the AirTag Tracker web UI to sign into Apple ID and enable Find My."
