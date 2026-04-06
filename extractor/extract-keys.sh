#!/usr/bin/env bash
# Extracts AirTag private keys from the macOS VM.
# Usage: extract-keys.sh [--vm-dir /path/to/osx-kvm] [--output /path/to/keys]
set -euo pipefail

VM_DIR="${AIRTAG_VM_DIR:-/var/lib/airtag-tracker/osx-kvm}"
OUTPUT_DIR="${AIRTAG_DATA_DIR:-/var/lib/airtag-tracker}/keys"
VM_SSH_PORT=2222
VM_USER="user"
VM_PASS_FILE="${AIRTAG_DATA_DIR:-/var/lib/airtag-tracker}/vm-password"
QEMU_PID=""

log() { echo "[$(date '+%H:%M:%S')] $*"; }

cleanup() {
  if [ -n "$QEMU_PID" ] && kill -0 "$QEMU_PID" 2>/dev/null; then
    log "Shutting down macOS VM..."
    # Graceful shutdown via SSH
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -p "$VM_SSH_PORT" \
      "$VM_USER@localhost" 'sudo shutdown -h now' 2>/dev/null || true
    # Wait up to 30s for graceful shutdown
    for _i in $(seq 1 30); do
      kill -0 "$QEMU_PID" 2>/dev/null || break
      sleep 1
    done
    # Force kill if still running
    kill -0 "$QEMU_PID" 2>/dev/null && kill "$QEMU_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

start_vm() {
  log "Starting macOS VM..."
  qemu-system-x86_64 \
    -enable-kvm -m 6144 \
    -cpu Penryn,kvm=on,vendor=GenuineIntel,+invtsc,vmware-cpuid-freq=on,+ssse3,+sse4.2,+popcnt,+avx,+aes,+xsave,+xsaveopt,check \
    -machine q35 \
    -device qemu-xhci,id=xhci \
    -device usb-kbd,bus=xhci.0 -device usb-tablet,bus=xhci.0 \
    -smp 4,cores=2 \
    -global ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off \
    -device isa-applesmc,osk="ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc" \
    -drive if=pflash,format=raw,readonly=on,file="$VM_DIR/OVMF_CODE.fd" \
    -drive if=pflash,format=raw,file="$VM_DIR/OVMF_VARS-1920x1080.fd" \
    -smbios type=2 \
    -device ich9-ahci,id=sata \
    -drive id=OpenCoreBoot,if=none,snapshot=on,format=qcow2,file="$VM_DIR/OpenCore/OpenCore.qcow2" \
    -device ide-hd,bus=sata.2,drive=OpenCoreBoot \
    -drive id=MacHDD,if=none,file="$VM_DIR/mac_hdd_ng.img",format=qcow2 \
    -device ide-hd,bus=sata.4,drive=MacHDD \
    -netdev user,id=net0,hostfwd=tcp::${VM_SSH_PORT}-:22 \
    -device vmxnet3,netdev=net0,id=net0,mac=52:54:00:c9:18:27 \
    -device vmware-svga \
    -display none \
    -daemonize \
    -pidfile /tmp/airtag-vm.pid \
    2>&1 || { log "Failed to start VM"; exit 1; }

  QEMU_PID=$(cat /tmp/airtag-vm.pid)
  log "VM started (PID: $QEMU_PID)"
}

wait_for_ssh() {
  log "Waiting for macOS to boot (this takes 1-3 minutes)..."
  for _i in $(seq 1 120); do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=3 -p "$VM_SSH_PORT" \
       "$VM_USER@localhost" 'echo ready' 2>/dev/null; then
      log "SSH is ready"
      return 0
    fi
    sleep 2
  done
  log "ERROR: VM did not become reachable via SSH"
  exit 1
}

extract_keys() {
  log "Unlocking keychain and extracting keys..."

  VM_PASS=""
  if [ -f "$VM_PASS_FILE" ]; then
    VM_PASS=$(cat "$VM_PASS_FILE")
  else
    log "ERROR: VM password file not found at $VM_PASS_FILE"
    exit 1
  fi

  # Upload the decryptor script
  scp -o StrictHostKeyChecking=no -P "$VM_SSH_PORT" \
    "$(dirname "$0")/airtag_decryptor.py" "$VM_USER@localhost:/tmp/" 2>/dev/null

  # Run extraction inside the VM
  ssh -o StrictHostKeyChecking=no -p "$VM_SSH_PORT" "$VM_USER@localhost" \
    "set -euo pipefail; security unlock-keychain -p '${VM_PASS}' ~/Library/Keychains/login.keychain-db 2>/dev/null || true; cd /tmp; python3 airtag_decryptor.py --rename-legacy --path=/tmp/airtag-export 2>&1; echo EXTRACTION_DONE"

  log "Copying extracted keys from VM..."
  mkdir -p "$OUTPUT_DIR"

  # Copy decrypted plists
  scp -o StrictHostKeyChecking=no -r -P "$VM_SSH_PORT" \
    "$VM_USER@localhost:/tmp/airtag-export/OwnedBeacons/" "/tmp/airtag-plists/" 2>/dev/null

  log "Converting plists to FindMy.py JSON format..."
  python3 "$(dirname "$0")/plist_to_findmy.py" /tmp/airtag-plists/ "$OUTPUT_DIR"

  rm -rf /tmp/airtag-plists/
  log "Keys extracted and saved to $OUTPUT_DIR"
}

# Main
start_vm
wait_for_ssh
extract_keys
log "Done. VM shutting down."
