// Pre-capture readiness gate.
//
// Resolves a structured report when ALL of the following are true (or each
// gate's individual cap is reached, whichever comes first):
//
//   - document.readyState === "complete"
//   - document.fonts.ready resolved (5s cap)
//   - all viewport-visible <img> elements: complete && naturalHeight > 0 (8s)
//   - all <video> elements: readyState >= HAVE_METADATA (5s)
//   - PerformanceObserver sees no new resource entries for 1.5s (15s cap)
//
// Hard ceiling: 30s total, regardless of individual gates.
//
// Each gate's outcome is recorded so the audit log can show what was
// actually awaited. We don't fail the capture on a timeout — partial
// readiness is still better than no capture at all.

(async () => {
  const HARD_CEILING_MS = 30_000;
  const start = performance.now();
  const ceilingHit = () => performance.now() - start > HARD_CEILING_MS;

  const report = {
    started_at: new Date().toISOString(),
    gates: [],
    hard_ceiling_ms: HARD_CEILING_MS,
  };

  function recordGate(name, ok, detail) {
    report.gates.push({
      name,
      ok,
      elapsed_ms: Math.round(performance.now() - start),
      detail: detail || null,
    });
  }

  async function withCap(name, capMs, factory) {
    return new Promise((resolve) => {
      let resolved = false;
      const timeoutId = setTimeout(() => {
        if (!resolved) {
          resolved = true;
          recordGate(name, false, "timeout");
          resolve();
        }
      }, capMs);
      Promise.resolve()
        .then(factory)
        .then((detail) => {
          if (!resolved) {
            resolved = true;
            clearTimeout(timeoutId);
            recordGate(name, true, detail || null);
            resolve();
          }
        })
        .catch((err) => {
          if (!resolved) {
            resolved = true;
            clearTimeout(timeoutId);
            recordGate(name, false, "error:" + (err && err.name) || "unknown");
            resolve();
          }
        });
    });
  }

  // Gate 1: document.readyState
  await withCap("ready_state", 5000, async () => {
    if (document.readyState === "complete") return "complete";
    await new Promise((r) => {
      window.addEventListener("load", () => r(), { once: true });
    });
    return document.readyState;
  });
  if (ceilingHit()) return finish();

  // Gate 2: fonts
  await withCap("fonts", 5000, async () => {
    if (!document.fonts) return "no-fonts-api";
    await document.fonts.ready;
    return "fonts:" + document.fonts.size;
  });
  if (ceilingHit()) return finish();

  // Gate 3: visible images
  await withCap("images", 8000, async () => {
    const tStart = performance.now();
    while (performance.now() - tStart < 8000 - 100) {
      const imgs = Array.from(document.images || []);
      if (imgs.length === 0) return "no-images";
      let pending = 0;
      for (const img of imgs) {
        const r = img.getBoundingClientRect();
        const inView = r.top < window.innerHeight && r.bottom > 0;
        if (!inView) continue;
        if (!img.complete || img.naturalHeight === 0) pending++;
      }
      if (pending === 0) return `images:${imgs.length} complete`;
      await new Promise((r) => setTimeout(r, 200));
    }
    return "images:still-pending";
  });
  if (ceilingHit()) return finish();

  // Gate 4: video metadata
  await withCap("video", 5000, async () => {
    const tStart = performance.now();
    while (performance.now() - tStart < 5000 - 100) {
      const vids = Array.from(document.querySelectorAll("video"));
      if (vids.length === 0) return "no-video";
      const pending = vids.filter((v) => v.readyState < 1).length;
      if (pending === 0) return `video:${vids.length} ready`;
      await new Promise((r) => setTimeout(r, 200));
    }
    return "video:still-pending";
  });
  if (ceilingHit()) return finish();

  // Gate 5: network-quiet (no new resource entries for 1.5s)
  await withCap("network_quiet", 15000, async () => {
    return await new Promise((resolve) => {
      const QUIET_MS = 1500;
      let lastSeen = performance.now();
      let count = 0;
      const observer = new PerformanceObserver((list) => {
        count += list.getEntries().length;
        lastSeen = performance.now();
      });
      try {
        observer.observe({ type: "resource", buffered: true });
      } catch (_) {
        resolve("no-observer");
        return;
      }
      const intervalId = setInterval(() => {
        if (performance.now() - lastSeen >= QUIET_MS) {
          observer.disconnect();
          clearInterval(intervalId);
          resolve(`quiet-after:${count}-resources`);
        }
      }, 250);
      // Hard inner cap so we never hold past the gate's 15s limit.
      setTimeout(() => {
        observer.disconnect();
        clearInterval(intervalId);
        resolve("noisy");
      }, 14000);
    });
  });

  return finish();

  function finish() {
    report.finished_at = new Date().toISOString();
    report.total_elapsed_ms = Math.round(performance.now() - start);
    report.hit_ceiling = ceilingHit();
    return report;
  }
})();
