#!/usr/bin/env bash
# provision.sh — idempotent setup of the QEMU VM directory.
#
# Run once on container start (before the server).  Safe to re-run: each step
# checks whether the target already exists before doing anything.
#
# Env vars:
#   AIRTAG_VM_DIR     — where VM disk images live  (default: /var/lib/airtag-tracker/osx-kvm)
#   AIRTAG_ASSETS_DIR — where bundled assets live   (default: /app/assets)

set -euo pipefail

VM_DIR="${AIRTAG_VM_DIR:-/var/lib/airtag-tracker/osx-kvm}"
ASSETS_DIR="${AIRTAG_ASSETS_DIR:-/app/assets}"

echo "[provision] VM_DIR     = $VM_DIR"
echo "[provision] ASSETS_DIR = $ASSETS_DIR"

# ---------------------------------------------------------------------------
# 1. Directory structure
# ---------------------------------------------------------------------------
mkdir -p "$VM_DIR/OpenCore"
echo "[provision] directories OK"

# ---------------------------------------------------------------------------
# 2. OVMF_CODE_4M.fd — read-only UEFI firmware (copied as-is)
# ---------------------------------------------------------------------------
OVMF_CODE_DST="$VM_DIR/OVMF_CODE_4M.fd"
if [ -f "$OVMF_CODE_DST" ]; then
    echo "[provision] OVMF_CODE_4M.fd already present — skip"
else
    echo "[provision] Copying OVMF_CODE_4M.fd ..."
    cp "$ASSETS_DIR/OVMF_CODE_4M.fd" "$OVMF_CODE_DST"
    echo "[provision] OVMF_CODE_4M.fd ready"
fi

# ---------------------------------------------------------------------------
# 3. OVMF_VARS-1920x1080.qcow2 — UEFI variable store (converted to qcow2 so
#    savevm can write snapshot state into the same file across restarts)
# ---------------------------------------------------------------------------
OVMF_VARS_DST="$VM_DIR/OVMF_VARS-1920x1080.qcow2"
if [ -f "$OVMF_VARS_DST" ]; then
    echo "[provision] OVMF_VARS-1920x1080.qcow2 already present — skip"
else
    echo "[provision] Converting OVMF_VARS-1920x1080.fd → qcow2 ..."
    qemu-img convert \
        -f raw \
        -O qcow2 \
        "$ASSETS_DIR/OVMF_VARS-1920x1080.fd" \
        "$OVMF_VARS_DST"
    echo "[provision] OVMF_VARS-1920x1080.qcow2 ready"
fi

# ---------------------------------------------------------------------------
# 4. OpenCore/OpenCore.qcow2 — pre-built OpenCore bootloader (fresh writable
#    copy so per-run NVRAM writes don't corrupt the bundled template)
# ---------------------------------------------------------------------------
OPENCORE_DST="$VM_DIR/OpenCore/OpenCore.qcow2"
if [ -f "$OPENCORE_DST" ]; then
    echo "[provision] OpenCore/OpenCore.qcow2 already present — skip"
else
    echo "[provision] Copying OpenCore.qcow2 template ..."
    qemu-img convert \
        -O qcow2 \
        "$ASSETS_DIR/OpenCore.qcow2" \
        "$OPENCORE_DST"
    echo "[provision] OpenCore/OpenCore.qcow2 ready"
fi

# ---------------------------------------------------------------------------
# 5. OpenCore/config.plist — OpenCore configuration
# ---------------------------------------------------------------------------
OPENCORE_CFG_DST="$VM_DIR/OpenCore/config.plist"
if [ -f "$OPENCORE_CFG_DST" ]; then
    echo "[provision] OpenCore/config.plist already present — skip"
else
    echo "[provision] Copying OpenCore-config.plist ..."
    cp "$ASSETS_DIR/OpenCore-config.plist" "$OPENCORE_CFG_DST"
    echo "[provision] OpenCore/config.plist ready"
fi

# ---------------------------------------------------------------------------
# BaseSystem is NOT downloaded here — the web UI handles that via
# POST /api/setup/download-macos.
# ---------------------------------------------------------------------------

echo "[provision] Done."
