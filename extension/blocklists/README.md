# Extension blocklists

These files are **byte-identical copies** of the canonical blocklists at
`app/static/blocklists/`. The extension can't reach into `app/static/` at
runtime (extensions are sandboxed), so the bundle ships its own copy.

A test in `tests/test_blocklist.py` asserts byte-identity. If they ever
drift, a tooling/build issue is the reason — fix that, don't paper over the
diff.

The extension converts `easylist-essentials.json` into Chrome
declarativeNetRequest rules at install time (see `background.js`).
