# nix-airtag-tracker

Automated AirTag location key extraction via a macOS VM.

## Requirements

- Linux with KVM (`/dev/kvm`)
- Docker + Docker Compose

## Quick Start

```bash
git clone <repo>
cd nix-airtag-tracker
docker compose up -d
```

Open http://localhost:8042 — the web UI will guide you through:
1. Downloading macOS Sonoma (~3 GB, one-time, from Apple)
2. Running the install (first time ~60 min, automatic)
3. Signing in with your Apple ID to extract AirTag keys

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AIRTAG_MACOS_VERSION` | `14` | macOS version (14 = Sonoma) |

VM disk images are stored in the `airtag-data` Docker named volume (inspect with `docker volume inspect airtag-data`).
To store them on a specific host path instead, replace `airtag-data:/data` in `compose.yaml` with a bind mount like `./data:/data`.

## Development

```bash
# Run tests
PYTHONPATH=server uv run --with pytest pytest tests/

# Rebuild container after Dockerfile changes
docker compose up --build -d
```
