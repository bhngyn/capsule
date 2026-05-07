# Capsule — web-evidence capture tool.
#
# Slim Debian Bookworm + Python 3.12. We install ONLY Chromium (not Firefox or
# WebKit, which Playwright's official base image bakes in by default) and bundle
# only the three font files we actually render in PDFs. This brings the runtime
# image down from ~2 GB (Playwright base + fonts-noto-cjk + fonts-noto) to
# roughly ~900 MB — important for investigators on metered or weak connections,
# who download the image as part of the dist bundle (see scripts/build-dist.sh).
#
# CLAUDE.md §3 calls for multi-arch (linux/amd64 + linux/arm64). python:3.12-
# slim-bookworm publishes both. Apple Silicon hosts pull the arm64 layer
# natively — no Rosetta drag on Chromium.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

# Single RUN to keep the image clean: no apt cache, no pip cache, no curl in
# the final image. Each step explained inline.
#
#  - apt deps split into two groups: runtime (kept) and build/fetch tools
#    (removed at the end of the layer).
#  - WeasyPrint pulls libcairo / libpango / libgdk-pixbuf / libharfbuzz /
#    fontconfig as RUNTIME shared libs; without these the per-item manifest
#    PDF and case_report PDF won't render.
#  - tini: PID 1 reaper so SIGINT/SIGTERM propagate cleanly to uvicorn.
#  - ffmpeg: yt-dlp's video+audio mux fallback.
#  - playwright install --with-deps chromium: pulls Chromium (and only Chromium —
#    not Firefox or WebKit) plus the apt packages Chromium needs (libnss3,
#    libatk1.0-0, libgbm1, etc.). Saves ~400–600 MB over the playwright/python
#    base image which bakes in all three browsers by default.
#  - Bundled fonts: full fonts-noto-cjk is ~280 MB and fonts-noto is ~40+ MB,
#    almost all of which we never render. We need three families:
#        Inter            — Latin (UI + PDF body text)  -> apt: fonts-inter (~3 MB pkg)
#        Noto Sans Arabic — Arabic (RTL)                -> apt: fonts-noto-core (~12 MB pkg)
#        Noto Sans JP     — Japanese                    -> curl single subset OTF (~5 MB)
#    Debian has no per-language Noto CJK package; the Sans JP subset OTF in
#    notofonts/noto-cjk is the smallest available unit covering JIS X 0208
#    plus essential kanji. Saved into /usr/share/fonts/truetype/capsule and
#    registered with fontconfig so WeasyPrint resolves "Inter", "Noto Sans JP",
#    and "Noto Sans Arabic" by family name.
#  - Chromium runtime libs are installed MANUALLY (not via `playwright install
#    --with-deps`), because --with-deps drags in libgl1-mesa-dri (~23 MB) which
#    pulls libllvm15 (~107 MB) plus speech-synth (libflite1, ~27 MB), Chinese
#    and Unicode fallback fonts (~52 MB), and emoji fonts. Headless Chromium
#    uses none of those. The list below mirrors Playwright's documented
#    bookworm Chromium dep list (https://playwright.dev/docs/browsers) minus
#    the GPU/audio/extra-fonts items.
#  - `playwright install chromium --only-shell`: ships ONLY the headless-shell
#    binary (~300 MB), not the full headed Chromium (~540 MB). Playwright 1.49+
#    auto-selects headless-shell when `p.chromium.launch(headless=True)` is
#    invoked, so no API change is needed.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        ca-certificates \
        fontconfig \
        fonts-inter \
        fonts-noto-core \
        libasound2 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libgbm1 \
        libgdk-pixbuf-2.0-0 \
        libglib2.0-0 \
        libharfbuzz0b \
        libicu72 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        shared-mime-info \
        curl; \
    pip install --no-cache-dir --upgrade pip; \
    pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "python-multipart>=0.0.20" \
        "playwright>=1.49,<1.50" \
        "cryptography>=44" \
        "httpx>=0.28" \
        "weasyprint>=63" \
        "babel>=2.16" \
        "yt-dlp>=2025.1.1"; \
    playwright install chromium --only-shell; \
    mkdir -p /usr/share/fonts/truetype/capsule; \
    curl -fsSL -o /usr/share/fonts/truetype/capsule/NotoSansJP-Regular.otf \
        "https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/JP/NotoSansJP-Regular.otf"; \
    fc-cache -f; \
    apt-get purge -y curl; \
    apt-get autoremove -y --purge; \
    # PYTHONDONTWRITEBYTECODE prevents *new* .pyc files but pip wheels ship
    # pre-built __pycache__ directories; they accumulate to ~200 MB on a full
    # install. Strip them — the runtime regenerates what it needs (or doesn't,
    # since PYTHONDONTWRITEBYTECODE is on).
    find /usr/local/lib/python3.12 -depth -type d -name __pycache__ -exec rm -rf '{}' +; \
    find /usr/local/lib/python3.12 -depth -type f -name '*.pyc' -delete; \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /root/.cache

WORKDIR /app

# Pyproject + app code.
COPY pyproject.toml ./
COPY app/ ./app/

# /downloads and /config are bind-mount targets. Pre-create so first launch
# doesn't fail if the host folders are empty / freshly created.
RUN mkdir -p /downloads /config

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
