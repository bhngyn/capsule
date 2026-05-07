# Capsule — web-evidence capture tool.
#
# Built on Microsoft's official Playwright Python image: it ships Chromium
# (matched to the playwright pip version), all the apt-level deps Chromium
# needs, and a sane Python 3.12 base. We add ffmpeg (yt-dlp uses it for the
# rare video+audio merge) and the FastAPI / cryptography / yt-dlp stack on
# top.
#
# CLAUDE.md §3 calls for multi-arch (linux/amd64 + linux/arm64). The
# Microsoft image publishes both, so a `docker buildx build --platform
# linux/arm64,linux/amd64` works without extra effort. Apple Silicon hosts
# pull the arm64 layer natively — no Rosetta drag on Chromium.
#
# Image size: ~2GB after Chromium + ffmpeg + Python deps. Documented in
# README.md.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: required for yt-dlp's video+audio mux fallback path.
# tini: PID 1 reaper so SIGINT/SIGTERM propagate cleanly to uvicorn.
# fonts-noto-cjk + fonts-noto: provide Japanese, Chinese, Korean, and
# Arabic glyph coverage for WeasyPrint-rendered PDFs (case_report.pdf
# and the per-item manifest PDF). Without these, RTL Arabic and CJK
# locales render as tofu boxes in evidence bundles.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg tini fonts-noto-cjk fonts-noto \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first so iteration on app/ doesn't bust this layer. Playwright
# is pinned to the exact version baked into the base image — mismatching
# them causes "Executable doesn't exist at /ms-playwright/chromium-*"
# errors at runtime.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
 && pip install \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.32" \
      "python-multipart>=0.0.20" \
      "playwright==1.49.0" \
      "cryptography>=44" \
      "httpx>=0.28" \
      "yt-dlp>=2025.1.1"

# App code last — edits to app/ rebuild only this layer (~1 second).
COPY app/ ./app/

# /downloads and /config are bind-mount targets. Pre-create so first launch
# doesn't fail if the host folders are empty / freshly created.
RUN mkdir -p /downloads /config

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
