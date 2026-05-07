"""Translation bundle loader.

Bundles live as flat key/value JSON in ``app/i18n/{lang}.json``. Plurals and
interpolation use ICU MessageFormat; the runtime is on the frontend
(``@formatjs/intl-messageformat``). The backend's job is just to read,
fallback-chain, and serve.

Loading is eager and cached: bundles are small (<100KB even with full
translations) and never change at runtime.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Mapping

from . import config


@lru_cache(maxsize=None)
def load(lang: str) -> Mapping[str, str]:
    """Load one bundle by language code, falling back to English on miss.

    Returns a flat dict of ICU MessageFormat strings.
    """
    primary = lang.split("-", 1)[0]
    candidates = (primary, config.DEFAULT_LANG)
    for code in candidates:
        path = config.I18N_DIR / f"{code}.json"
        if path.is_file():
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(f"No i18n bundle found for {lang!r} or fallback {config.DEFAULT_LANG!r}")


def merged_with_fallback(lang: str) -> dict[str, str]:
    """Return a bundle with English filling in any keys missing in ``lang``.

    This guarantees the frontend never sees an undefined key, even with a
    half-translated locale.
    """
    en_bundle = dict(load(config.DEFAULT_LANG))
    if lang.split("-", 1)[0] == config.DEFAULT_LANG:
        return en_bundle
    target = dict(load(lang))
    en_bundle.update(target)
    return en_bundle
