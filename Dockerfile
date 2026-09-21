FROM python:3.12-slim

WORKDIR /app

# ffmpeg first — large, rarely changes, caches independently of app code.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies only — cached until pyproject.toml changes.
COPY pyproject.toml ./
RUN pip install --no-cache-dir yt-dlp==2026.8.19

# Copy source and install the package itself (no-deps: deps already above).
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

RUN useradd -m app && mkdir -p /app/data /app/tmp /app/cookies \
    && chown -R app:app /app
USER app

# Phase 5: health = fresh poll heartbeat, judged by the status CLI exit code.
HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD ["python", "-m", "bookmedia.status", "--db", "/app/data/archive.db"]

CMD ["python", "-m", "bookmedia"]
