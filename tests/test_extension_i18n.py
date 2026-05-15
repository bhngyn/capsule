"""Browser-extension i18n parity tests — CLAUDE.md §4.5.

The Capsule browser extension at ``extension/`` uses the WebExtension
i18n format (one ``messages.json`` per locale under ``_locales/<lang>/``)
rather than the main app's flat ICU bundles. These tests enforce the
same parity guarantees:

* every English key is present in every other locale's bundle
* placeholder definitions match (``$HOST$``, ``$COUNT$``, …)
* translated ``message`` values actually differ from the English source
  (except for brand-name keys and a small allow-list of strings that
  are intentionally identical across locales)

The plural family ``sentCount_*`` is special: WebExtension messages.json
has no ICU support, so we store one key per CLDR plural category and
``Intl.PluralRules`` selects at runtime. English keeps ``one`` + ``other``;
Japanese keeps ``other`` only; Arabic keeps all six (``zero``, ``one``,
``two``, ``few``, ``many``, ``other``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# WebExtension placeholders: $UPPER_OR_DIGIT$. The reference is at
# https://developer.chrome.com/docs/extensions/reference/api/i18n#placeholders.
_PLACEHOLDER_RE = re.compile(r"\$[A-Z0-9_]+\$")

EXT_LOCALES = Path(__file__).parent.parent / "extension" / "_locales"

# Keys that are allowed to be identical across all locales — brand names,
# technical placeholders, and the popup title that just renders "Capsule".
_IDENTICAL_ALLOWED: frozenset[str] = frozenset(
    {
        "extName",
        "popupTitle",
        "pairTokenPlaceholder",  # "capsule-…" literal token prefix
    }
)

# CLDR plural-category keys. English has these; other locales may have a
# subset (Japanese) or a superset (Arabic).
_PLURAL_SUFFIXES = {"zero", "one", "two", "few", "many", "other"}
_PLURAL_STEMS = {"sentCount"}


def _is_plural_key(key: str) -> bool:
    if "_" not in key:
        return False
    stem, _, suffix = key.rpartition("_")
    return stem in _PLURAL_STEMS and suffix in _PLURAL_SUFFIXES


def _load(locale: str) -> dict:
    return json.loads((EXT_LOCALES / locale / "messages.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def en() -> dict:
    return _load("en")


# --- key parity ----------------------------------------------------------


@pytest.mark.parametrize("locale", ["ja", "ar"])
def test_locale_has_every_non_plural_english_key(en: dict, locale: str) -> None:
    """Every non-plural English key must appear in every translated bundle."""
    target = _load(locale)
    non_plural = {k for k in en if not _is_plural_key(k)}
    missing = non_plural - set(target)
    assert not missing, f"{locale} missing keys: {sorted(missing)}"


def test_japanese_has_only_plural_other(en: dict) -> None:
    """Japanese (CLDR: only ``other``) carries exactly ``sentCount_other``."""
    ja = _load("ja")
    plural_keys = {k for k in ja if _is_plural_key(k)}
    assert plural_keys == {"sentCount_other"}, plural_keys


def test_arabic_has_all_six_plural_forms(en: dict) -> None:
    """Arabic (CLDR: all six forms) carries every ``sentCount_*`` category."""
    ar = _load("ar")
    expected = {f"sentCount_{suffix}" for suffix in _PLURAL_SUFFIXES}
    plural_keys = {k for k in ar if _is_plural_key(k)}
    assert plural_keys == expected, sorted(plural_keys ^ expected)


# --- placeholder parity --------------------------------------------------


@pytest.mark.parametrize("locale", ["ja", "ar"])
def test_placeholder_names_match_english(en: dict, locale: str) -> None:
    """Placeholder definitions must match English for every shared key."""
    target = _load(locale)
    mismatches: list[str] = []
    for key, en_entry in en.items():
        en_holders = set((en_entry.get("placeholders") or {}).keys())
        if key not in target or not en_holders:
            continue
        target_holders = set((target[key].get("placeholders") or {}).keys())
        if en_holders != target_holders:
            mismatches.append(
                f"{key}: en={sorted(en_holders)} {locale}={sorted(target_holders)}"
            )
    assert not mismatches, "\n".join(mismatches)


@pytest.mark.parametrize("locale", ["ja", "ar"])
def test_placeholder_tokens_appear_in_message(en: dict, locale: str) -> None:
    """If English uses $TOKEN$ in a message, the translation must too —
    losing a placeholder would silently drop runtime substitutions.

    Plural-form keys where the count is encoded linguistically (Arabic's
    "one"/"two"/"zero" forms, English's "one" form) are exempt: it's
    natural to write "ارسال التقاط واحد" without the literal `$COUNT$`.
    Those forms fire only when the count exactly matches the rule, so
    losing the substitution doesn't drop information.
    """
    target = _load(locale)
    misses: list[str] = []
    for key, en_entry in en.items():
        if key not in target:
            continue
        # Skip plural-form keys where the count is linguistically implicit.
        if _is_plural_key(key) and key.rsplit("_", 1)[-1] in {"zero", "one", "two"}:
            continue
        en_tokens = set(_PLACEHOLDER_RE.findall(en_entry["message"]))
        target_msg = target[key]["message"]
        for token in en_tokens:
            if token not in target_msg:
                misses.append(f"{locale}/{key}: missing token {token}")
    assert not misses, "\n".join(misses)


# --- translation freshness ----------------------------------------------


@pytest.mark.parametrize("locale", ["ja", "ar"])
def test_translations_actually_differ_from_english(en: dict, locale: str) -> None:
    """Every translated message must differ from the English source unless
    explicitly allow-listed (brand name, technical placeholder)."""
    target = _load(locale)
    same: list[str] = []
    for key, en_entry in en.items():
        if key in _IDENTICAL_ALLOWED or key not in target:
            continue
        if en_entry["message"] == target[key]["message"]:
            same.append(f"{locale}/{key}")
    assert not same, (
        f"Translations identical to English (add to _IDENTICAL_ALLOWED if "
        f"intentional): {same}"
    )


# --- structural sanity --------------------------------------------------


@pytest.mark.parametrize("locale", ["en", "ja", "ar"])
def test_every_entry_has_message_field(locale: str) -> None:
    bundle = _load(locale)
    bad = [k for k, v in bundle.items() if not isinstance(v.get("message"), str)]
    assert not bad, f"{locale}: entries missing string `message`: {bad}"


@pytest.mark.parametrize("locale", ["en", "ja", "ar"])
def test_descriptions_are_strings(locale: str) -> None:
    """Descriptions help downstream translators; they should not be empty."""
    bundle = _load(locale)
    bad = [
        k
        for k, v in bundle.items()
        if "description" in v and not isinstance(v["description"], str)
    ]
    assert not bad, f"{locale}: entries with non-string `description`: {bad}"
