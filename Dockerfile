FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        qemu-system-x86 \
        qemu-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        sshpass \
        openssh-client \
        novnc \
        websockify \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server/ server/
COPY assets/ assets/
COPY scripts/ scripts/
RUN chmod +x scripts/*.sh

ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
ENV PYTHONUNBUFFERED=1

# Runtime defaults so the image runs standalone (single /data volume, VM on).
# Mirrors compose.yaml; override any of these with `docker run -e ...`.
ENV AIRTAG_VM_ENABLED=1
ENV AIRTAG_DATA_DIR=/data
ENV AIRTAG_VM_DIR=/data/osx-kvm
ENV AIRTAG_ASSETS_DIR=/app/assets

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
