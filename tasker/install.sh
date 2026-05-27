#!/usr/bin/env bash
set -euo pipefail

if ! command -v adb &>/dev/null; then
    echo "adb not found — install Android platform-tools first."
    exit 1
fi
adb start-server &>/dev/null

# ---- connect over WiFi if no device is present ----
devices=$(adb devices | awk '/\tdevice$/{print $1}')
count=$(echo "$devices" | grep -c . || true)

if [[ $count -eq 0 ]]; then
    read -rp "No device found. Enter phone IP for wireless ADB (or press Enter to wait for USB): " phone_ip
    if [[ -n "$phone_ip" ]]; then
        adb connect "${phone_ip}:5555"
        devices=$(adb devices | awk '/\tdevice$/{print $1}')
        count=$(echo "$devices" | grep -c . || true)
    else
        echo "Connect phone via USB (USB debugging on) and press Enter."
        read -r
        devices=$(adb devices | awk '/\tdevice$/{print $1}')
        count=$(echo "$devices" | grep -c . || true)
    fi
    [[ $count -eq 0 ]] && echo "No device found. Exiting." && exit 1
fi

if [[ $count -eq 1 ]]; then
    device="$devices"
else
    echo "Connected devices:"
    i=1; while IFS= read -r d; do echo "  $i) $d"; ((i++)); done <<< "$devices"
    read -rp "Select number: " sel
    device=$(echo "$devices" | sed -n "${sel}p")
fi

read -rp "Server address as seen from the phone (e.g. http://192.168.0.100:8042): " addr
relay_url="${addr%/}/api/vm/apple-signin/sms-relay"

src="$(cd "$(dirname "$0")" && pwd)/AirTag_2FA_Relay.prf.xml"
tmp=$(mktemp /tmp/AirTag_Tasker_XXXXXX.prf.xml)
trap 'rm -f "$tmp"' EXIT

sed "s|<Str sr=\"arg2\" ve=\"3\"></Str>|<Str sr=\"arg2\" ve=\"3\">${relay_url}</Str>|" "$src" > "$tmp"

adb -s "$device" push "$tmp" /sdcard/Tasker/AirTag_2FA_Relay.prf.xml
adb -s "$device" shell am start -a android.intent.action.VIEW \
    -d "file:///sdcard/Tasker/AirTag_2FA_Relay.prf.xml" \
    -t "text/xml" net.dinglisch.android.taskerm

echo "Done — tap OK in Tasker to confirm."
