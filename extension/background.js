// Capsule extension service worker.
//
// All network traffic goes to the user's local Capsule install on
// localhost:8080 (or whatever they configured during pairing). The
// extension never talks to the public internet, never persists cookies
// to chrome.storage, and never logs cookie values.
//
// Hardening pass:
//   - cookies are gathered from EVERY cookie store (default, container
//     tabs, incognito) so partitioned third-party cookies survive.
//   - tab_context envelope (UA, viewport, scroll, timezone, referrer,
//     color-scheme) ships alongside cookies so the backend Chromium
//     mirrors what the user saw.
//   - per-origin localStorage / sessionStorage snapshot.
//   - click-time DOM snapshot (outerHTML + structural counts).
//   - pre-capture readiness gate (fonts, images, video, network-quiet).
//   - X-Extension-Id header on every authenticated request, so a token
//     bound to one extension id can't be replayed from another browser.
//   - declarativeNetRequest blocklist applied to the user's tab so the
//     captured network log isn't drowned in trackers (matches what the
//     backend will then re-block for its canonical capture).
//   - optional chrome.debugger HAR (yellow banner; off by default).

const STORAGE_KEYS = {
  serverUrl: "capsule.server_url",
  token: "capsule.token",
  tokenId: "capsule.token_id",
  fingerprint: "capsule.server_fingerprint",
  label: "capsule.label",
  activeCaseId: "capsule.active_case_id",
  liveCaptureEnabled: "capsule.live_capture_enabled",
  blockAdsEnabled: "capsule.block_ads_enabled",
  realHarEnabled: "capsule.real_har_enabled",
  cookiePersistence: "capsule.cookie_persistence",
  captureMode: "capsule.captureMode",
};

const DEFAULT_SERVER = "http://localhost:8080";
const BLOCKLIST_RULESET_ID = "capsule-blocklist";

// --- pairing storage -------------------------------------------------------

async function getPairing() {
  const data = await chrome.storage.local.get([
    STORAGE_KEYS.serverUrl,
    STORAGE_KEYS.token,
    STORAGE_KEYS.tokenId,
    STORAGE_KEYS.fingerprint,
    STORAGE_KEYS.label,
  ]);
  return {
    serverUrl: data[STORAGE_KEYS.serverUrl] || DEFAULT_SERVER,
    token: data[STORAGE_KEYS.token] || null,
    tokenId: data[STORAGE_KEYS.tokenId] || null,
    fingerprint: data[STORAGE_KEYS.fingerprint] || null,
    label: data[STORAGE_KEYS.label] || null,
  };
}

async function setPairing({ serverUrl, token, tokenId, fingerprint, label }) {
  await chrome.storage.local.set({
    [STORAGE_KEYS.serverUrl]: serverUrl,
    [STORAGE_KEYS.token]: token,
    [STORAGE_KEYS.tokenId]: tokenId || null,
    [STORAGE_KEYS.fingerprint]: fingerprint,
    [STORAGE_KEYS.label]: label,
  });
}

async function clearPairing() {
  await chrome.storage.local.remove([
    STORAGE_KEYS.serverUrl,
    STORAGE_KEYS.token,
    STORAGE_KEYS.tokenId,
    STORAGE_KEYS.fingerprint,
    STORAGE_KEYS.label,
  ]);
}

// --- HTTP helpers ----------------------------------------------------------

function joinUrl(base, path) {
  return base.replace(/\/+$/, "") + path;
}

async function authedFetch(path, init = {}) {
  const { serverUrl, token } = await getPairing();
  if (!token) {
    throw new Error("not_paired");
  }
  const headers = new Headers(init.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  // Hardening: bind the request to this extension's id so a leaked token
  // from another browser instance is rejected by the server.
  headers.set("X-Extension-Id", chrome.runtime.id || "");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(joinUrl(serverUrl, path), { ...init, headers });
  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(`http_${response.status}`);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return response.json();
}

async function publicFetch(serverUrl, path, init = {}) {
  const response = await fetch(joinUrl(serverUrl, path), init);
  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(`http_${response.status}`);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return response.json();
}

// --- pairing flow ---------------------------------------------------------

async function pairWithServer({ serverUrl, token, tokenId, label }) {
  const version = await fetch(joinUrl(serverUrl, "/api/system/version"));
  if (!version.ok) {
    throw new Error("server_unreachable");
  }
  const versionBody = await version.json();
  const fingerprint = versionBody.signing_key_fingerprint;
  if (!fingerprint) {
    throw new Error("missing_fingerprint");
  }
  // Probe the token by listing cases — the cheapest authed call we have.
  const probe = await fetch(joinUrl(serverUrl, "/api/extension/cases"), {
    headers: {
      "Authorization": `Bearer ${token}`,
      "X-Extension-Id": chrome.runtime.id || "",
    },
  });
  if (!probe.ok) {
    if (probe.status === 403) throw new Error("extension_id_mismatch");
    throw new Error("invalid_token");
  }
  await setPairing({
    serverUrl,
    token,
    tokenId: tokenId || null,
    fingerprint,
    label: label || "",
  });
  return { fingerprint };
}

async function checkServerIdentity() {
  const { serverUrl, fingerprint } = await getPairing();
  if (!serverUrl || !fingerprint) return { ok: false, reason: "not_paired" };
  try {
    const body = await publicFetch(serverUrl, "/api/system/version");
    if (body.signing_key_fingerprint !== fingerprint) {
      return { ok: false, reason: "fingerprint_mismatch", actual: body.signing_key_fingerprint };
    }
    return { ok: true, version: body };
  } catch (err) {
    return { ok: false, reason: "server_unreachable" };
  }
}

async function rotateToken() {
  const { serverUrl, tokenId } = await getPairing();
  if (!tokenId) throw new Error("no_token_id");
  const resp = await fetch(joinUrl(serverUrl, `/api/extension/pair/${tokenId}/rotate`), {
    method: "POST",
  });
  if (!resp.ok) {
    throw new Error(`http_${resp.status}`);
  }
  const body = await resp.json();
  await setPairing({
    serverUrl,
    token: body.token,
    tokenId: body.token_id,
    fingerprint: body.server_fingerprint,
    label: body.label || "",
  });
  return { token_id: body.token_id, label: body.label };
}

// --- cookie collection ----------------------------------------------------

async function listCookieStores() {
  // Browsers return one or more cookie stores: the default plus one per
  // container tab / incognito window. We iterate every store so an
  // investigator using Firefox Multi-Account containers gets cookies for
  // the actual container they were authenticated in.
  if (!chrome.cookies || !chrome.cookies.getAllCookieStores) {
    return [{ id: "0" }];
  }
  return await new Promise((resolve) => {
    try {
      chrome.cookies.getAllCookieStores((stores) => {
        resolve(stores && stores.length ? stores : [{ id: "0" }]);
      });
    } catch (_) {
      resolve([{ id: "0" }]);
    }
  });
}

async function collectCookiesForUrls(urls) {
  // Collect across all cookie stores; deduplicate by (storeId, name, domain,
  // path, partitionKey) so partitioned third-party cookies survive too.
  const seen = new Map();
  const stores = await listCookieStores();
  const skippedUrls = [];

  for (const url of urls) {
    let anyError = false;
    for (const store of stores) {
      try {
        const entries = await chrome.cookies.getAll({
          url,
          storeId: store.id,
        });
        for (const c of entries) {
          const partKey = c.partitionKey
            ? JSON.stringify(c.partitionKey)
            : "";
          const key = `${store.id}|${c.name}|${c.domain}|${c.path}|${partKey}`;
          if (!seen.has(key)) {
            seen.set(key, {
              name: c.name,
              value: c.value,
              domain: c.domain,
              path: c.path,
              expirationDate: c.expirationDate ?? null,
              secure: !!c.secure,
              httpOnly: !!c.httpOnly,
              hostOnly: !!c.hostOnly,
              sameSite: c.sameSite || null,
              storeId: store.id,
              partitionKey: c.partitionKey || null,
            });
          }
        }
      } catch (e) {
        anyError = true;
      }
    }
    if (anyError) skippedUrls.push(url);
  }
  return {
    cookies: Array.from(seen.values()),
    skipped_urls: skippedUrls,
    store_count: stores.length,
  };
}

// --- live capture ---------------------------------------------------------

async function captureMHTML(tabId) {
  if (!chrome.pageCapture || !chrome.pageCapture.saveAsMHTML) return null;
  return await new Promise((resolve) => {
    try {
      chrome.pageCapture.saveAsMHTML({ tabId }, async (blob) => {
        if (!blob) return resolve(null);
        const buf = await blob.arrayBuffer();
        resolve({ b64: arrayBufferToBase64(buf), size: blob.size });
      });
    } catch (e) {
      resolve(null);
    }
  });
}

async function captureScreenshot(tabId) {
  // captureVisibleTab grabs only the viewport. Full-page capture is the
  // backend's job (Playwright `page.screenshot(full_page=True)`); this
  // viewport shot proves what the investigator was actually seeing.
  return new Promise((resolve) => {
    try {
      chrome.tabs.captureVisibleTab(null, { format: "png" }, (dataUrl) => {
        if (!dataUrl) return resolve(null);
        const idx = dataUrl.indexOf(",");
        resolve(idx >= 0 ? dataUrl.slice(idx + 1) : null);
      });
    } catch (e) {
      resolve(null);
    }
  });
}

async function captureEnvironment(tab) {
  // Investigator's working environment — the non-reproducible context the
  // canonical clean-Chromium capture deliberately discards. Recorded so
  // a court reviewer can see exactly how the page rendered for the
  // investigator.
  return {
    url: tab?.url ?? null,
    title: tab?.title ?? null,
    userAgent: navigator.userAgent,
    language: navigator.language,
    languages: navigator.languages,
    platform: navigator.platform,
    timeISO: new Date().toISOString(),
    captured_in: "browser_extension",
    schema_version: 2,
  };
}

async function captureTabContext(tab) {
  // Structured tab_context the backend Chromium uses to mirror the user's
  // environment. Runs in MAIN world so navigator.* / window.* reflect the
  // actual page (not the service worker).
  if (!chrome.scripting || !tab?.id) return null;
  try {
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: () => {
        const tz = (() => {
          try { return Intl.DateTimeFormat().resolvedOptions().timeZone; }
          catch (_) { return null; }
        })();
        const colorScheme = (() => {
          try {
            if (matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
            if (matchMedia("(prefers-color-scheme: light)").matches) return "light";
            return "no-preference";
          } catch (_) { return null; }
        })();
        const reducedMotion = (() => {
          try { return matchMedia("(prefers-reduced-motion: reduce)").matches; }
          catch (_) { return null; }
        })();
        return {
          user_agent: navigator.userAgent,
          ua_brands: (navigator.userAgentData && navigator.userAgentData.brands) || null,
          viewport: {
            width: window.innerWidth,
            height: window.innerHeight,
            device_scale_factor: window.devicePixelRatio || 1,
          },
          scroll: { x: window.scrollX || 0, y: window.scrollY || 0 },
          timezone: tz,
          language: navigator.language,
          languages: navigator.languages,
          color_scheme: colorScheme,
          reduced_motion: reducedMotion,
          referrer: document.referrer || null,
          document_title: document.title,
          ready_state: document.readyState,
          captured_at: new Date().toISOString(),
          page_url: location.href,
        };
      },
    });
    return result || null;
  } catch (e) {
    return null;
  }
}

async function captureSessionState(tab) {
  // Per-origin localStorage + sessionStorage. Iterates same-origin frames
  // so a logged-in iframe (e.g. an embedded auth gate) is also captured.
  if (!chrome.scripting || !tab?.id) return null;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      world: "MAIN",
      files: ["content/storage-probe.js"],
    });
    // Deduplicate by origin (top-level frame + same-origin iframes share
    // storage; we only need one entry per origin).
    const byOrigin = new Map();
    for (const r of results) {
      if (r && r.result && r.result.origin) {
        if (!byOrigin.has(r.result.origin)) {
          byOrigin.set(r.result.origin, r.result);
        }
      }
    }
    return Array.from(byOrigin.values());
  } catch (e) {
    return null;
  }
}

async function captureDomSnapshot(tab) {
  if (!chrome.scripting || !tab?.id) return null;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      files: ["content/dom-snapshot.js"],
    });
    return results && results[0] ? results[0].result : null;
  } catch (e) {
    return null;
  }
}

async function waitForReadiness(tab) {
  if (!chrome.scripting || !tab?.id) return { gates: [], hit_ceiling: true };
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      files: ["content/readiness.js"],
    });
    return results && results[0] ? results[0].result : null;
  } catch (e) {
    return null;
  }
}

async function captureHar(tabId) {
  // Lightweight Resource Timing approximation. Real HAR ships when the
  // user enables the chrome.debugger toggle (see `attachDebuggerHar`).
  if (!chrome.scripting || !chrome.scripting.executeScript) return null;
  try {
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const entries = performance.getEntriesByType("resource").map((e) => ({
          name: e.name,
          startTime: e.startTime,
          duration: e.duration,
          transferSize: e.transferSize,
          encodedBodySize: e.encodedBodySize,
          decodedBodySize: e.decodedBodySize,
          initiatorType: e.initiatorType,
        }));
        return {
          page: { url: location.href, title: document.title },
          entries,
        };
      },
    });
    return result || null;
  } catch (e) {
    return null;
  }
}

async function attachDebuggerHar(tabId, durationMs = 3000) {
  // Real HAR via chrome.debugger. Chrome shows a persistent yellow
  // "<extension> is debugging this browser" banner while attached — that
  // is intentional and makes the elevated capability visible to the user.
  // Off by default; opt-in via the Settings toggle.
  if (!chrome.debugger) return null;
  const target = { tabId };
  const events = [];
  const onEvent = (source, method, params) => {
    if (source.tabId !== tabId) return;
    if (method.startsWith("Network.")) {
      events.push({ method, params, t: Date.now() });
    }
  };
  try {
    await new Promise((resolve, reject) => {
      chrome.debugger.attach(target, "1.3", () => {
        const err = chrome.runtime.lastError;
        if (err) return reject(new Error(err.message));
        resolve();
      });
    });
    chrome.debugger.onEvent.addListener(onEvent);
    await chrome.debugger.sendCommand(target, "Network.enable");
    await new Promise((r) => setTimeout(r, durationMs));
    chrome.debugger.onEvent.removeListener(onEvent);
    // Race the detach against a 3-second timeout so a hung detach never
    // blocks the artifact chain — resolve (not reject) on timeout so the
    // caller still gets the collected events.
    await Promise.race([
      new Promise((resolve) => {
        chrome.debugger.detach(target, () => resolve());
      }),
      new Promise((resolve) => setTimeout(resolve, 3000)),
    ]);
  } catch (e) {
    try { chrome.debugger.onEvent.removeListener(onEvent); } catch (_) {}
    try { chrome.debugger.detach(target); } catch (_) {}
    return null;
  }
  return { source: "chrome.debugger.Network", events };
}

async function buildLiveCaptureForTab(tab, { realHar }) {
  if (!tab || !tab.id) return { live: null, warnings: ["no_tab"] };
  const warnings = [];

  // Wait for readiness BEFORE grabbing artifacts so we capture a stable page.
  const readiness = await waitForReadiness(tab).catch(() => null);
  if (!readiness) warnings.push("readiness_failed");

  const tabContext = await captureTabContext(tab);
  if (!tabContext) warnings.push("tab_context_failed");
  else if (readiness) tabContext.readiness_report = readiness;

  // Race each artifact against a per-op cap so a hanging fetch never
  // blocks the whole submission.
  const withTimeout = (p, ms, label) =>
    Promise.race([
      Promise.resolve(p).catch(() => null),
      new Promise((r) => setTimeout(() => { warnings.push(`timeout:${label}`); r(null); }, ms)),
    ]);

  const [mhtmlB64, screenshotB64, environment, har, sessionState, domSnapshot, debuggerHar] =
    await Promise.all([
      withTimeout(captureMHTML(tab.id), 8000, "mhtml"),
      withTimeout(captureScreenshot(tab.id), 5000, "screenshot"),
      withTimeout(captureEnvironment(tab), 3000, "environment"),
      withTimeout(captureHar(tab.id), 3000, "har_resource_timing"),
      withTimeout(captureSessionState(tab), 5000, "session_state"),
      withTimeout(captureDomSnapshot(tab), 5000, "dom_snapshot"),
      realHar ? withTimeout(attachDebuggerHar(tab.id), 5000, "har_debugger") : Promise.resolve(null),
    ]);

  // Unpack MHTML result — captureMHTML now returns {b64, size} or null.
  const mhtmlResult = mhtmlB64; // variable holds the raw Promise result
  const mhtmlBase64 = mhtmlResult ? mhtmlResult.b64 : null;
  const MHTML_WARN_BYTES = 50 * 1024 * 1024; // 50 MB
  if (!mhtmlBase64) warnings.push("mhtml_unavailable");
  else if (mhtmlResult.size > MHTML_WARN_BYTES) warnings.push("mhtml_large");
  if (!screenshotB64) warnings.push("screenshot_unavailable");
  if (!environment) warnings.push("environment_unavailable");

  // Cap DOM snapshot HTML at 5 MB before base64-encoding to prevent OOM on
  // pathologically large pages.
  const MAX_DOM_HTML_BYTES = 5 * 1024 * 1024; // 5 MB
  let domHtml = domSnapshot ? (domSnapshot.outer_html || "") : null;
  let domTruncated = false;
  if (domHtml && domHtml.length > MAX_DOM_HTML_BYTES) {
    domHtml = domHtml.slice(0, MAX_DOM_HTML_BYTES);
    domTruncated = true;
  }
  const dom_html_b64 = domHtml
    ? arrayBufferToBase64(new TextEncoder().encode(domHtml).buffer)
    : null;

  const live = {
    url: tab.url,
    mhtml_b64: mhtmlBase64,
    screenshot_b64: screenshotB64,
    har: debuggerHar || har,
    environment,
    tab_context: tabContext,
    session_state: sessionState,
    dom_snapshot_html_b64: dom_html_b64,
    dom_snapshot_meta: domSnapshot
      ? { ...domSnapshot.meta, truncated: domTruncated }
      : null,
    capture_warnings: warnings,
  };
  return { live, warnings };
}

// --- declarativeNetRequest blocklist --------------------------------------

async function ensureBlocklistInstalled() {
  // Read the bundled blocklist JSON, convert to DNR rules, install once.
  // Idempotent: replaces the prior session of the same ruleset.
  if (!chrome.declarativeNetRequest) return false;
  try {
    const url = chrome.runtime.getURL("blocklists/easylist-essentials.json");
    const resp = await fetch(url);
    const body = await resp.json();
    const hosts = body.blocked_hosts || [];
    // DNR rule IDs must be positive integers; assign sequentially.
    const rules = hosts.map((host, i) => ({
      id: i + 1,
      priority: 1,
      action: { type: "block" },
      condition: {
        requestDomains: [host],
        resourceTypes: [
          "script",
          "image",
          "xmlhttprequest",
          "ping",
          "media",
          "websocket",
          "sub_frame",
        ],
      },
    }));
    // Replace the entire dynamic ruleset.
    const existing = await chrome.declarativeNetRequest.getDynamicRules();
    const existingIds = existing.map((r) => r.id);
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: existingIds,
      addRules: rules,
    });
    return true;
  } catch (e) {
    return false;
  }
}

async function getBlockAdsEnabled() {
  const data = await chrome.storage.local.get([STORAGE_KEYS.blockAdsEnabled]);
  // Default ON.
  return data[STORAGE_KEYS.blockAdsEnabled] !== false;
}

async function applyBlockAdsState() {
  const enabled = await getBlockAdsEnabled();
  if (enabled) {
    await ensureBlocklistInstalled();
  } else {
    // Remove all dynamic rules.
    try {
      const existing = await chrome.declarativeNetRequest.getDynamicRules();
      await chrome.declarativeNetRequest.updateDynamicRules({
        removeRuleIds: existing.map((r) => r.id),
        addRules: [],
      });
    } catch (_) {}
  }
}

chrome.runtime.onInstalled.addListener(() => {
  applyBlockAdsState();
});

chrome.runtime.onStartup.addListener(() => {
  applyBlockAdsState();
});

// --- submission -----------------------------------------------------------

async function submitCapture({ caseId, urls, includeLiveCapture, captureMode = null }) {
  const { cookies, skipped_urls, store_count } = await collectCookiesForUrls(urls);

  let liveCaptures = [];
  let captureWarnings = [];
  if (includeLiveCapture) {
    const settings = await chrome.storage.local.get([STORAGE_KEYS.realHarEnabled]);
    const realHar = !!settings[STORAGE_KEYS.realHarEnabled];
    const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (activeTab && urls.includes(activeTab.url)) {
      const { live, warnings } = await buildLiveCaptureForTab(activeTab, { realHar });
      if (live) liveCaptures = [live];
      captureWarnings = warnings;
    }
  }

  const cookiePersistence = (
    await chrome.storage.local.get([STORAGE_KEYS.cookiePersistence])
  )[STORAGE_KEYS.cookiePersistence] || "case";

  const body = {
    case_id: caseId,
    urls,
    cookies,
    live_captures: liveCaptures,
    cookie_persistence: cookiePersistence,
  };
  if (captureMode) body.capture_mode = captureMode;

  const resp = await authedFetch("/api/extension/capture", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return {
    ...resp,
    capture_warnings: captureWarnings,
    skipped_urls,
    cookie_store_count: store_count,
  };
}

async function syncCookiesOnly({ caseId, url }) {
  const { cookies } = await collectCookiesForUrls([url]);
  return await authedFetch("/api/cookies/json", {
    method: "POST",
    body: JSON.stringify({ case_id: caseId, cookies, target_url: url }),
  });
}

// --- helpers --------------------------------------------------------------

function arrayBufferToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

// --- message bus ----------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      switch (msg?.type) {
        case "pair":
          sendResponse({ ok: true, ...(await pairWithServer(msg.payload)) });
          return;
        case "clear-pairing":
          await clearPairing();
          sendResponse({ ok: true });
          return;
        case "get-pairing":
          sendResponse({ ok: true, pairing: await getPairing() });
          return;
        case "rotate-token":
          sendResponse({ ok: true, ...(await rotateToken()) });
          return;
        case "check-identity":
          sendResponse(await checkServerIdentity());
          return;
        case "list-cases":
          sendResponse({ ok: true, ...(await authedFetch("/api/extension/cases")) });
          return;
        case "set-active-case":
          await chrome.storage.local.set({ [STORAGE_KEYS.activeCaseId]: msg.payload.caseId });
          sendResponse({ ok: true });
          return;
        case "get-active-case": {
          const data = await chrome.storage.local.get([STORAGE_KEYS.activeCaseId]);
          sendResponse({ ok: true, caseId: data[STORAGE_KEYS.activeCaseId] || null });
          return;
        }
        case "get-settings": {
          const data = await chrome.storage.local.get([
            STORAGE_KEYS.blockAdsEnabled,
            STORAGE_KEYS.realHarEnabled,
            STORAGE_KEYS.cookiePersistence,
            STORAGE_KEYS.liveCaptureEnabled,
            STORAGE_KEYS.captureMode,
          ]);
          sendResponse({
            ok: true,
            settings: {
              block_ads: data[STORAGE_KEYS.blockAdsEnabled] !== false,
              real_har: !!data[STORAGE_KEYS.realHarEnabled],
              cookie_persistence: data[STORAGE_KEYS.cookiePersistence] || "case",
              live_capture: !!data[STORAGE_KEYS.liveCaptureEnabled],
              capture_mode: data[STORAGE_KEYS.captureMode] || null,
            },
          });
          return;
        }
        case "set-settings": {
          const updates = {};
          if (msg.payload.block_ads !== undefined)
            updates[STORAGE_KEYS.blockAdsEnabled] = !!msg.payload.block_ads;
          if (msg.payload.real_har !== undefined)
            updates[STORAGE_KEYS.realHarEnabled] = !!msg.payload.real_har;
          if (msg.payload.cookie_persistence !== undefined)
            updates[STORAGE_KEYS.cookiePersistence] = msg.payload.cookie_persistence;
          if (msg.payload.live_capture !== undefined)
            updates[STORAGE_KEYS.liveCaptureEnabled] = !!msg.payload.live_capture;
          if ("capture_mode" in msg.payload)
            updates[STORAGE_KEYS.captureMode] = msg.payload.capture_mode || null;
          await chrome.storage.local.set(updates);
          if (msg.payload.block_ads !== undefined) {
            await applyBlockAdsState();
          }
          sendResponse({ ok: true });
          return;
        }
        case "submit-capture":
          sendResponse({ ok: true, ...(await submitCapture({
            caseId: msg.payload.caseId,
            urls: msg.payload.urls,
            includeLiveCapture: msg.payload.includeLiveCapture,
            captureMode: msg.payload.captureMode || null,
          })) });
          return;
        case "sync-cookies":
          sendResponse({ ok: true, ...(await syncCookiesOnly(msg.payload)) });
          return;
        default:
          sendResponse({ ok: false, error: "unknown_message" });
      }
    } catch (err) {
      sendResponse({
        ok: false,
        error: err?.message || String(err),
        status: err?.status,
        detail: err?.detail,
      });
    }
  })();
  return true; // async response
});
