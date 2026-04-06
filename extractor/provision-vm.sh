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
  if [ -d "$VM_DIR/.git" ]; then
    git pull
  else
    git clone --depth 1 https://github.com/kholia/OSX-KVM.git "$VM_DIR/tmp-osx-kvm"
    # Move contents up (OSX-KVM clones into a subdir)
    mv "$VM_DIR/tmp-osx-kvm"/* "$VM_DIR/tmp-osx-kvm"/.* "$VM_DIR/" 2>/dev/null || true
    rmdir "$VM_DIR/tmp-osx-kvm" 2>/dev/null || true
  fi
fi

# Download macOS installer
if [ ! -f "$VM_DIR/BaseSystem.dmg" ]; then
  log "Downloading macOS $MACOS_VERSION installer..."
  python3 "$VM_DIR/fetch-macOS-v2.py" --action download --board-id Mac-4B682C642B45593E --os latest
fi

# Convert to bootable image
if [ ! -f "$VM_DIR/BaseSystem.img" ]; then
  log "Converting BaseSystem.dmg..."
  qemu-img convert -O raw "$VM_DIR/BaseSystem.dmg" "$VM_DIR/BaseSystem.img" 2>/dev/null || \
    dmg2img "$VM_DIR/BaseSystem.dmg" "$VM_DIR/BaseSystem.img"
fi

# Create main disk (80GB is enough for macOS + Find My)
if [ ! -f "$VM_DIR/mac_hdd_ng.img" ]; then
  log "Creating 80GB disk image..."
  qemu-img create -f qcow2 "$VM_DIR/mac_hdd_ng.img" 80G
fi

# Copy OVMF firmware files
if [ ! -f "$VM_DIR/OVMF_CODE.fd" ]; then
  log "Setting up UEFI firmware..."
  cp "$VM_DIR/OVMF_CODE.fd" "$VM_DIR/OVMF_CODE.fd" 2>/dev/null || true
  cp "$VM_DIR/OVMF_VARS-1920x1080.fd" "$VM_DIR/OVMF_VARS-1920x1080.fd" 2>/dev/null || true
fi

log ""
log "=== VM Provisioned ==="
log ""
log "Next steps (one-time manual setup via VNC):"
log "  1. Start VM with VNC: AIRTAG_VM_DIR=$VM_DIR qemu-system-x86_64 \\"
log "       -enable-kvm -m 6144 -cpu Penryn,kvm=on,vendor=GenuineIntel,+invtsc,vmware-cpuid-freq=on \\"
log "       -machine q35 -smp 4,cores=2 \\"
log "       -device qemu-xhci,id=xhci -device usb-kbd,bus=xhci.0 -device usb-tablet,bus=xhci.0 \\"
log "       -global ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off \\"
log "       -drive if=pflash,format=raw,readonly=on,file=$VM_DIR/OVMF_CODE.fd \\"
log "       -drive if=pflash,format=raw,file=$VM_DIR/OVMF_VARS-1920x1080.fd \\"
log "       -smbios type=2 -device isa-applesmc,osk='ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc' \\"
log "       -device ich9-ahci,id=sata \\"
log "       -drive id=OpenCoreBoot,if=none,snapshot=on,format=qcow2,file=$VM_DIR/OpenCore/OpenCore.qcow2 \\"
log "       -device ide-hd,bus=sata.2,drive=OpenCoreBoot \\"
log "       -drive id=InstallMedia,if=none,file=$VM_DIR/BaseSystem.img,format=raw \\"
log "       -device ide-hd,bus=sata.3,drive=InstallMedia \\"
log "       -drive id=MacHDD,if=none,file=$VM_DIR/mac_hdd_ng.img,format=qcow2 \\"
log "       -device ide-hd,bus=sata.4,drive=MacHDD \\"
log "       -netdev user,id=net0,hostfwd=tcp::2222-:22 \\"
log "       -device vmxnet3,netdev=net0 -device vmware-svga \\"
log "       -vnc :1"
log "  2. Connect VNC to nasx:5901"
log "  3. Install macOS, create user 'user', sign into Apple ID, enable Find My"
log "  4. Enable SSH: System Settings > General > Sharing > Remote Login"
log "  5. Save the VM user password to /var/lib/airtag-tracker/vm-password"
log "  6. Shut down the VM"
