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

# Install Python deps before copying server code so this layer is cached.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server/ server/

ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
ENV PYTHONUNBUFFERED=1

CMD ["uv", "run", "python", "server/tracker.py"]
