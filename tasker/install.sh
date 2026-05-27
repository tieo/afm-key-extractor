#!/usr/bin/env bash
set -euo pipefail

die() { echo "Error: $*" >&2; exit 1; }

if ! command -v adb &>/dev/null; then
    die "adb not found — install Android platform-tools first."
fi
adb start-server &>/dev/null

# ---- connect ----
devices=$(adb devices | awk '/\tdevice$/{print $1}')
count=$(echo "$devices" | grep -c . || true)

if [[ $count -eq 0 ]]; then
    read -rp "No device found. Enter phone IP for wireless ADB (or press Enter for USB): " phone_ip
    if [[ -n "$phone_ip" ]]; then
        adb connect "${phone_ip}:5555" | grep -q "connected" || die "Could not connect to ${phone_ip}:5555"
    else
        echo "Connect phone via USB and press Enter."
        read -r
    fi
    devices=$(adb devices | awk '/\tdevice$/{print $1}')
    count=$(echo "$devices" | grep -c . || true)
    [[ $count -eq 0 ]] && die "No device found."
fi

if [[ $count -eq 1 ]]; then
    device="$devices"
else
    echo "Connected devices:"
    i=1; while IFS= read -r d; do echo "  $i) $d"; ((i++)); done <<< "$devices"
    read -rp "Select number: " sel
    device=$(echo "$devices" | sed -n "${sel}p")
    [[ -z "$device" ]] && die "Invalid selection."
fi
echo "Using device: $device"

# ---- check Tasker is installed ----
adb -s "$device" shell pm list packages | grep -q "net.dinglisch.android.taskerm" \
    || die "Tasker (net.dinglisch.android.taskerm) not installed on device."

# ---- server address ----
read -rp "Server address as seen from the phone (e.g. http://192.168.0.100:8042): " addr
[[ -z "$addr" ]] && die "No address given."
[[ "$addr" =~ ^https?:// ]] || die "Address must start with http:// or https://"
relay_url="${addr%/}/api/vm/apple-signin/sms-relay"

# ---- build patched XML ----
src="$(cd "$(dirname "$0")" && pwd)/AirTag_2FA_Relay.prf.xml"
tmp=$(mktemp /tmp/AirTag_Tasker_XXXXXX.prf.xml)
trap 'rm -f "$tmp"' EXIT

sed "s|<Str sr=\"arg2\" ve=\"3\"></Str>|<Str sr=\"arg2\" ve=\"3\">${relay_url}</Str>|" "$src" > "$tmp"
grep -q "$relay_url" "$tmp" || die "URL injection failed."

# ---- push ----
dest=/sdcard/Tasker/AirTag_2FA_Relay.prf.xml
adb -s "$device" shell mkdir -p /sdcard/Tasker
adb -s "$device" push "$tmp" "$dest"
adb -s "$device" shell ls "$dest" &>/dev/null || die "File not found on device after push."

# ---- grant storage access so Tasker can read the file ----
# Android 10+ scoped storage blocks file:// reads for apps without All Files Access.
adb -s "$device" shell appops set net.dinglisch.android.taskerm \
    android:manage_external_storage allow 2>/dev/null || true

# ---- try to open Tasker import dialog ----
result=$(adb -s "$device" shell am start \
    -a android.intent.action.VIEW \
    -d "file://${dest}" \
    -t "text/xml" \
    net.dinglisch.android.taskerm 2>&1)
echo "$result"

if echo "$result" | grep -qi "error\|exception\|unable"; then
    echo ""
    echo "Automatic import failed (Android 10+ scoped storage restriction)."
else
    echo ""
    echo "If Tasker opened but didn't show a dialog, use the manual step below."
fi

echo ""
echo "Manual import (one time):"
echo "  Tasker → long-press PROFILES tab → Import Profile"
echo "  File is at: $dest"
