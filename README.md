# nix-airtag-tracker

Extracts AirTag private keys from iCloud via an automated macOS VM.
Keys are compatible with [OpenTagViewer](https://github.com/OpenTagViewer).

**Requires:** Linux with KVM, Docker + Compose.

---

## Quick start

```bash
cp .env.example .env   # fill in Apple ID + passcode
docker compose up -d
```

Open **http://localhost:8042**

---

## First time only (~60–90 min, fully automatic)

1. Click **Download macOS** — fetches Sonoma recovery from Apple (~3 GB)
2. Click **Start Install** — installs macOS and saves a golden VM snapshot

After that, the golden image is reused on every run. You never do this again.

---

## Extracting keys (~6–8 min per run)

Click **Start Extraction** (or set `AIRTAG_AUTO_RUN=true` to run on a schedule).

The flow signs into Apple ID, enables iCloud Keychain, syncs OwnedBeacons, extracts keys, shuts down.

**2FA:** if Apple requires a code, a prompt appears in the UI. Enter it, or automate it with Tasker (see below) — then it's fully unattended.

When done, **Download Keys** appears in the header → ZIP of all key JSONs.

---

## Configuration (`.env`)

| Variable | Description |
|----------|-------------|
| `AIRTAG_APPLE_EMAIL` | Apple ID email |
| `AIRTAG_APPLE_PASSWORD` | Apple ID password |
| `AIRTAG_SMS_PHONE_SUFFIX` | Last 4+ digits of trusted phone number for SMS 2FA |
| `AIRTAG_IPHONE_PASSCODE` | iPhone passcode — needed once to unlock iCloud Keychain on a new Mac |
| `AIRTAG_AUTO_RUN` | `true` = extract keys automatically on a schedule |
| `AIRTAG_POLL_INTERVAL` | Seconds between auto-runs (default `900` = 15 min) |

After changing `.env`: `docker compose up -d --force-recreate`

---

## Automating 2FA with Tasker

Import [`tasker/AirTag_2FA_Relay.prf.xml`](tasker/AirTag_2FA_Relay.prf.xml) — it forwards every Apple SMS to the server, which extracts the OTP automatically.

**After importing:** open the task in Tasker → set the HTTP Request URL to:
```
https://<your-server>/api/vm/apple-signin/sms-relay
```

**Import via ADB** (one tap to confirm):
```bash
adb push tasker/AirTag_2FA_Relay.prf.xml /sdcard/Tasker/AirTag_2FA_Relay.prf.xml
adb shell am start -a android.intent.action.VIEW \
  -d "file:///sdcard/Tasker/AirTag_2FA_Relay.prf.xml" \
  -t "text/xml" net.dinglisch.android.taskerm
```

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/automation/start-install` | Start install flow |
| `POST` | `/api/automation/start-runtime` | Start extraction run |
| `POST` | `/api/automation/abort` | Abort running flow |
| `GET` | `/api/automation/status` | Current state |
| `POST` | `/api/vm/apple-signin/sms-relay` | Submit raw Apple SMS (Tasker posts here) |
| `POST` | `/api/vm/apple-signin/2fa` | Submit 2FA code manually `{"code":"123456"}` |
| `GET` | `/api/keys/zip` | Download all keys as ZIP |
| `GET` | `/api/events` | SSE stream |
