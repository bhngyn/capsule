// Capsule extension service worker.
//
// All network traffic goes to the user's local Capsule install on
// localhost:8080 (or whatever they configured during pairing). The
// extension never talks to the public internet, never persists cookies
// to chrome.storage, and never logs cookie values.

const STORAGE_KEYS = {
  serverUrl: "capsule.server_url",
  token: "capsule.token",
  fingerprint: "capsule.server_fingerprint",
  label: "capsule.label",
  activeCaseId: "capsule.active_case_id",
  liveCaptureEnabled: "capsule.live_capture_enabled",
};

const DEFAULT_SERVER = "http://localhost:8080";

// --- pairing storage -------------------------------------------------------

async function getPairing() {
  const data = await chrome.storage.local.get([
    STORAGE_KEYS.serverUrl,
    STORAGE_KEYS.token,
    STORAGE_KEYS.fingerprint,
    STORAGE_KEYS.label,
  ]);
  return {
    serverUrl: data[STORAGE_KEYS.serverUrl] || DEFAULT_SERVER,
    token: data[STORAGE_KEYS.token] || null,
    fingerprint: data[STORAGE_KEYS.fingerprint] || null,
    label: data[STORAGE_KEYS.label] || null,
  };
}

async function setPairing({ serverUrl, token, fingerprint, label }) {
  await chrome.storage.local.set({
    [STORAGE_KEYS.serverUrl]: serverUrl,
    [STORAGE_KEYS.token]: token,
    [STORAGE_KEYS.fingerprint]: fingerprint,
    [STORAGE_KEYS.label]: label,
  });
}

async function clearPairing() {
  await chrome.storage.local.remove([
    STORAGE_KEYS.serverUrl,
    STORAGE_KEYS.token,
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

async function pairWithServer({ serverUrl, token, label }) {
  // Verify the server fingerprint by calling /api/system/version with the
  // token. If it succeeds, we know the server holds the corresponding
  // hash; the fingerprint becomes our pin against future server swaps.
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
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!probe.ok) {
    throw new Error("invalid_token");
  }
  await setPairing({ serverUrl, token, fingerprint, label: label || "" });
  return { fingerprint };
}

async function checkServerIdentity() {
  // Bumped on each popup open to surface a server-key mismatch (e.g.
  // someone reinstalled the container and minted a new keypair).
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

// --- cookie collection ----------------------------------------------------

async function collectCookiesForUrls(urls) {
  // Browsers expose chrome.cookies.getAll({url}) per URL — including the
  // HttpOnly cookies that document.cookie cannot see. We deduplicate by
  // {name, domain, path} so the eventual Netscape file doesn't double-up.
  const seen = new Map();
  for (const url of urls) {
    let entries = [];
    try {
      entries = await chrome.cookies.getAll({ url });
    } catch (e) {
      // No host permission for this URL — skip silently. The popup
      // surfaces a hint to grant <all_urls> when this happens.
      continue;
    }
    for (const c of entries) {
      const key = `${c.name}${c.domain}${c.path}`;
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
        });
      }
    }
  }
  return Array.from(seen.values());
}

// --- live capture ---------------------------------------------------------

async function captureMHTML(tabId) {
  if (!chrome.pageCapture || !chrome.pageCapture.saveAsMHTML) return null;
  return await new Promise((resolve) => {
    try {
      chrome.pageCapture.saveAsMHTML({ tabId }, async (blob) => {
        if (!blob) return resolve(null);
        const buf = await blob.arrayBuffer();
        resolve(arrayBufferToBase64(buf));
      });
    } catch (e) {
      resolve(null);
    }
  });
}

async function captureScreenshot(tabId) {
  // captureVisibleTab grabs only the viewport. Full-page capture via the
  // debugger API is more invasive (requires user consent prompt) — we
  // lean on the visible-tab capture for v1 and let the canonical capture
  // own the full-page shot.
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
    schema_version: 1,
  };
}

async function captureHar(tabId) {
  // Real HAR requires DevTools to be open. Approximation: ask the page
  // for its performance entries via an injected content script.
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

async function buildLiveCaptureForTab(tab) {
  if (!tab || !tab.id) return null;
  const [mhtmlB64, screenshotB64, environment, har] = await Promise.all([
    captureMHTML(tab.id),
    captureScreenshot(tab.id),
    captureEnvironment(tab),
    captureHar(tab.id),
  ]);
  if (!mhtmlB64 && !screenshotB64 && !environment && !har) return null;
  return {
    url: tab.url,
    mhtml_b64: mhtmlB64,
    screenshot_b64: screenshotB64,
    har,
    environment,
  };
}

// --- submission -----------------------------------------------------------

async function submitCapture({ caseId, urls, includeLiveCapture }) {
  const cookies = await collectCookiesForUrls(urls);

  let liveCaptures = [];
  if (includeLiveCapture) {
    // Live capture only works for the active tab today — we'd need to
    // open each URL to capture it, which is invasive. Investigators who
    // want bulk captures should toggle live capture OFF.
    const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (activeTab && urls.includes(activeTab.url)) {
      const live = await buildLiveCaptureForTab(activeTab);
      if (live) liveCaptures = [live];
    }
  }

  return await authedFetch("/api/extension/capture", {
    method: "POST",
    body: JSON.stringify({
      case_id: caseId,
      urls,
      cookies,
      live_captures: liveCaptures,
    }),
  });
}

async function syncCookiesOnly({ caseId, url }) {
  const cookies = await collectCookiesForUrls([url]);
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
        case "submit-capture":
          sendResponse({ ok: true, ...(await submitCapture(msg.payload)) });
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
