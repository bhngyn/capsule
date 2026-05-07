# Capsule blocklists

Single source of truth for the ad/tracker blocklist and the cookie/consent
banner CSS hide layer. Both are used by:

- **Backend** (`app/blocklist.py`, `app/banner_hide.py`) — applied to the
  canonical Playwright + browsertrix capture.
- **Extension** (`extension/blocklists/`) — applied to the live tab the
  investigator is on, via `declarativeNetRequest`.

The extension's copy under `extension/blocklists/` is a verbatim duplicate.
A test in `tests/test_blocklist.py` asserts byte-identity between the two so
they can never silently drift.

## Files

| File                          | Purpose                                                   | Used by              |
|-------------------------------|-----------------------------------------------------------|----------------------|
| `easylist-essentials.json`    | Curated ad/tracker host list, conservative high-signal subset of EasyList | backend + extension |
| `banner-hide.css`             | CSS selectors that hide common cookie/consent banners. Visual-only — never modifies the DOM. | backend only |

## Editing

When updating the host list, bump the `version` field. The version surfaces
in the audit log on every blocked-request entry so a court reviewer can
reproduce the exact ruleset that was active at capture time.

The host list is intentionally small (a few hundred entries, well under
declarativeNetRequest's 30k limit) and covers the highest-signal domains.
We do **not** ship the full EasyList — that's tens of thousands of rules and
includes pattern matchers that can mis-fire on first-party sites.
