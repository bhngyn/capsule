"""Runtime configuration. Centralises every path and env-derived setting."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent

STATIC_DIR = APP_DIR / "static"
I18N_DIR = APP_DIR / "i18n"

# Bind-mount roots inside the container. Override only for local dev outside Docker.
DOWNLOADS_DIR = Path(os.environ.get("CAPSULE_DOWNLOADS_DIR", "/downloads"))
CONFIG_DIR = Path(os.environ.get("CAPSULE_CONFIG_DIR", "/config"))

# Host-side path of the bind mount that backs ``DOWNLOADS_DIR``. The
# container can't ``open`` the host file manager, so the UI falls back to
# copying the path for the user to paste into Finder/Explorer — and the
# container path (``/downloads``) is useless there. The launcher scripts
# pass this in via ``-e CAPSULE_HOST_DOWNLOADS_DIR=...``. Empty when
# unset; the API surfaces ``None`` and the UI keeps showing the container
# path as a degraded fallback.
HOST_DOWNLOADS_DIR = os.environ.get("CAPSULE_HOST_DOWNLOADS_DIR", "").strip() or None

DEFAULT_LANG = "en"
# Bundles ship for en/ar (translated) and es/fr (stubbed — values mirror EN
# until translation lands; merged_with_fallback guarantees no missing keys).
SUPPORTED_LANGS = ("en", "ar", "es", "fr")
RTL_LANGS = frozenset({"ar", "he", "fa", "ur"})


def is_rtl(lang: str) -> bool:
    """Return True if the locale is right-to-left."""
    return lang.split("-", 1)[0] in RTL_LANGS
