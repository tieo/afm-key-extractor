# nix-airtag-tracker

Automated AirTag location key extraction via a macOS VM running under QEMU/KVM.
Extracted keys are compatible with [OpenTagViewer](https://github.com/OpenTagViewer).

## Requirements

- Linux with KVM (`/dev/kvm`)
- Docker + Docker Compose

## Quick Start

```bash
git clone <repo>
cd nix-airtag-tracker
cp .env.example .env        # fill in your Apple ID
docker compose up -d
```

Open **http://localhost:8042** — the UI walks you through:

1. **First-time setup** — downloads macOS Sonoma recovery (~3 GB, from Apple, one-time)
2. **Install flow** — automated macOS install + golden image creation (~45–90 min, one-time)
3. **Runtime flow** — signs into Apple ID, syncs iCloud Keychain, extracts AirTag keys (~6–8 min per run)

Once at least one runtime run has completed, a **Download Keys** button appears in the header that gives you a ZIP of all extracted JSON key files.

## Configuration

Copy `.env.example` to `.env` and fill in your values:

| Variable | Default | Description |
|----------|---------|-------------|
| `AIRTAG_APPLE_EMAIL` | — | Apple ID email |
| `AIRTAG_APPLE_PASSWORD` | — | Apple ID password (app-specific password recommended) |
| `AIRTAG_SMS_PHONE_SUFFIX` | — | Last 4+ digits of the phone number for SMS 2FA. If Apple shows multiple trusted numbers, picks the right one. |
| `AIRTAG_IPHONE_PASSCODE` | — | iPhone passcode. Required the first time a new Mac signs into iCloud — macOS shows "Some iCloud Data Isn't Syncing" and needs this to enable iCloud Keychain. |
| `AIRTAG_AUTO_RUN` | `false` | Set to `true` to trigger extraction automatically every `AIRTAG_POLL_INTERVAL` seconds. |
| `AIRTAG_POLL_INTERVAL` | `900` | Auto-run interval in seconds (default 15 min). |
| `AIRTAG_MACOS_VERSION` | `14` | macOS version in the VM (14 = Sonoma). |
| `AIRTAG_PORT` | `8042` | API/UI port. |
| `AIRTAG_VNC_WS_PORT` | `6901` | noVNC WebSocket port (live VM view). |

## Two-Factor Authentication

When Apple requires 2FA, the runtime flow pauses at the **"Two-Factor Authentication Required"** screen and waits for a code. You can:

- **Enter it manually** — type the 6-digit code into the UI form and click Verify.
- **Automate it** — the flow emits an SSE event when paused and accepts a code via the API:
  ```
  POST /api/twofa/submit
  {"code": "123456"}
  ```
  Tools like [Tasker](https://tasker.joaoapps.com/) on Android can intercept the Apple SMS, parse the code, and POST it automatically — fully hands-free operation.

Setting `AIRTAG_SMS_PHONE_SUFFIX` ensures Apple sends the code to the right number when you have multiple trusted devices.

## Automatic Scheduling

Set `AIRTAG_AUTO_RUN=true` in `.env` (and `--force-recreate` the container once to pick it up):

```bash
docker compose up -d --force-recreate
```

The server will automatically start a new runtime run every `AIRTAG_POLL_INTERVAL` seconds (default 15 min) whenever no flow is currently running and a golden image exists. Combined with Tasker for 2FA forwarding, this is fully unattended.

## Key File Format

Each extracted key is a JSON file in `/data/keys/` (inside the container):

```json
{
  "type": "accessory",
  "master_key": "...",
  "skn": "...",
  "sks": "...",
  "paired_at": "2025-03-29T10:00:35+00:00",
  "name": "My AirTag",
  "identifier": "..."
}
```

Download all keys at once: `GET /api/keys/zip` (or the **Download Keys** button in the UI).

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/automation/status` | Current flow state |
| `POST` | `/api/automation/start-install` | Start the install flow |
| `POST` | `/api/automation/start-runtime` | Start a runtime/extraction run |
| `POST` | `/api/automation/abort` | Abort the running flow |
| `POST` | `/api/twofa/submit` | Submit 2FA code `{"code":"123456"}` |
| `POST` | `/api/twofa/request-sms` | Request SMS code instead of device prompt |
| `GET` | `/api/keys/` | List extracted key files |
| `GET` | `/api/keys/zip` | Download all keys as ZIP |
| `GET` | `/api/keys/{filename}` | Download a single key JSON |
| `GET` | `/api/log` | Recent activity log entries |
| `GET` | `/api/events` | SSE stream (state changes + log) |

## VNC

A live view of the VM is available at **http://localhost:6901** (noVNC) or via the **Open VM** button in the UI header.

## Development

```bash
# Run tests
PYTHONPATH=server uv run --with pytest pytest tests/

# Rebuild container after Dockerfile changes
docker compose up --build -d

# Code changes in server/ are picked up by a plain restart (bind-mounted):
docker compose restart airtag-tracker
```
