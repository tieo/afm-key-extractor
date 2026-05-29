# AFM Key Extractor

Extracts AirTag private keys from iCloud via an automated macOS VM.
Keys are compatible with [OpenTagViewer](https://github.com/parawanderer/OpenTagViewer) and my fork [TagHistory](https://github.com/tieo/TagHistory).

> [!WARNING]
> This project is not affiliated with Apple Inc. in any capacity.

**Requires:** Linux with KVM, Docker + Compose.

---

## Quick start (one-shot, from GHCR)

Paste this and it pulls the image, runs it, waits for the UI to come up, and opens it in your browser. Apple ID goes in the UI on first open.

```bash
docker run -d --name afm-key-extractor \
  --device /dev/kvm -p 8042:8042 -v afm-data:/data \
  ghcr.io/tieo/afm-key-extractor:latest && \
  until curl -sf http://localhost:8042/ >/dev/null; do sleep 1; done && \
  xdg-open http://localhost:8042
```

## Quick start (from source, with `.env`)

```bash
git clone https://github.com/tieo/afm-key-extractor && cd afm-key-extractor
cp .env.example .env   # fill in Apple ID + passcode
docker compose up -d
```

Open **http://localhost:8042**

---

## First time only (fully automatic)

1. Click **Download macOS** — fetches Sonoma recovery from Apple (~850 MB DMG → ~3 GB unpacked). Bandwidth-bound, then ~20 s to convert.
2. Click **Start Install** — installs macOS and saves a golden VM snapshot. Plan on **1–4 hours on QEMU** depending on host CPU; the on-screen "x hours remaining" estimate consistently lies and the bar appears stuck around 93 % for most of the run — that's normal.

After that, the golden image is reused on every run. You never do this again.

---

## Extracting keys

A clean run from start to keys is **~7–10 min** on an account with AirTags: boot from golden (~1 min), Apple ID sign-in + 2FA (~2–3 min), Find My enable (~1 min), iCloud sync of OwnedBeacons (~2–4 min), extract + shutdown (~1 min).

The iCloud sync wait has a 30-min hard ceiling and burns the whole thing if your account has no AirTags (no data to sync ⇒ wait times out). Measured 40 min in that empty-account case.

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

Forwards every Apple SMS to the server — fully unattended 2FA.

```bash
bash tasker/install.sh
```

Asks for the server address, patches the URL into the profile, and pushes it to the phone.

Due to Android 10+ scoped storage, the import dialog may not open automatically. If so, one manual step in Tasker:

> Long-press **PROFILES** tab → **Import Profile** → pick `/sdcard/Tasker/AirTag_2FA_Relay.prf.xml`

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

---

## License

[MIT License](LICENSE). Provided "as is", without warranty; use at your own risk.

Running macOS in a VM on non-Apple hardware, and automating Apple ID sign-in, may conflict with Apple's license agreement and account terms.
