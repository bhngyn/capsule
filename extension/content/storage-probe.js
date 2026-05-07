// Per-origin localStorage / sessionStorage probe.
//
// Runs in the page's main world (chrome.scripting world: 'MAIN') so it can
// read same-origin storage. The investigator's authenticated session may
// be partly held in localStorage (JWTs) — without these, the backend's
// re-fetch may render as logged-out even with valid cookies.
//
// Returned shape (single origin — top-level frame only; the background
// re-runs this for each same-origin sub-frame separately):
//   {
//     origin: <string>,
//     local_storage: { key: value, ... },
//     session_storage: { key: value, ... },
//     captured_at: <ISO 8601>
//   }
//
// Hard cap on output size: 1 MB per storage area, 256 KB per value. Oversize
// values are truncated and a `__capsule_truncated__` sentinel is added so
// the backend audit trail can record the truncation.

(() => {
  const MAX_AREA_BYTES = 1024 * 1024;
  const MAX_VALUE_BYTES = 256 * 1024;

  function dumpArea(storage) {
    const out = {};
    let total = 0;
    let truncated = false;
    try {
      for (let i = 0; i < storage.length; i++) {
        const key = storage.key(i);
        if (key === null) continue;
        const raw = storage.getItem(key);
        if (raw === null) continue;
        let value = raw;
        if (raw.length > MAX_VALUE_BYTES) {
          value = raw.slice(0, MAX_VALUE_BYTES) + "[…truncated]";
          truncated = true;
        }
        if (total + value.length > MAX_AREA_BYTES) {
          truncated = true;
          break;
        }
        out[key] = value;
        total += value.length;
      }
    } catch (e) {
      // SecurityError if the page disabled storage. Caller sees an empty obj.
    }
    if (truncated) out.__capsule_truncated__ = true;
    return out;
  }

  return {
    origin: location.origin,
    local_storage: dumpArea(window.localStorage),
    session_storage: dumpArea(window.sessionStorage),
    captured_at: new Date().toISOString(),
  };
})();
