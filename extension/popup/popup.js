// Capsule extension popup. Vanilla JS — small surface, no framework.

const $ = (id) => document.getElementById(id);

const screens = {
  unpaired: $("screen-unpaired"),
  paired: $("screen-paired"),
  result: $("screen-result"),
  error: $("screen-error"),
};

function show(name) {
  for (const k of Object.keys(screens)) screens[k].hidden = k !== name;
}

async function send(type, payload) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, payload }, (resp) => resolve(resp || { ok: false, error: "no_response" }));
  });
}

// --- i18n ----------------------------------------------------------------

function t(key, substitutions) {
  try {
    return chrome.i18n.getMessage(key, substitutions) || key;
  } catch (_) {
    return key;
  }
}

// Plural lookup keyed off the browser UI language. WebExtension messages.json
// has no native ICU support, so we store one key per CLDR plural category
// (sentCount_one, sentCount_other, etc.) and pick at runtime. Arabic carries
// all six forms; Japanese carries only "other".
function tPlural(stem, count) {
  const sub = [String(count)];
  let rule = "other";
  try {
    const lang = chrome.i18n.getUILanguage() || "en";
    rule = new Intl.PluralRules(lang).select(count);
  } catch (_) { /* fall back to other */ }
  const tried = chrome.i18n.getMessage(`${stem}_${rule}`, sub);
  if (tried) return tried;
  return chrome.i18n.getMessage(`${stem}_other`, sub) || `${stem}_${rule}`;
}

// Walks data-i18n*  attributes and applies translations to textContent /
// placeholder / aria-label / title. For keys that legitimately contain
// inline markup (pair-page steps, privacy notice), use data-i18n-html.
function i18nApply(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => {
    const msg = t(el.getAttribute("data-i18n"));
    if (msg) el.textContent = msg;
  });
  root.querySelectorAll("[data-i18n-html]").forEach((el) => {
    const msg = t(el.getAttribute("data-i18n-html"));
    if (msg) el.innerHTML = msg;
  });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const msg = t(el.getAttribute("data-i18n-placeholder"));
    if (msg) el.setAttribute("placeholder", msg);
  });
  root.querySelectorAll("[data-i18n-aria-label]").forEach((el) => {
    const msg = t(el.getAttribute("data-i18n-aria-label"));
    if (msg) el.setAttribute("aria-label", msg);
  });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => {
    const msg = t(el.getAttribute("data-i18n-title"));
    if (msg) el.setAttribute("title", msg);
  });
}

function setStatus(state, key) {
  const pill = $("status-pill");
  pill.dataset.state = state;
  pill.querySelector(".status-text").textContent = t(key);
}

function showError(message) {
  $("error-message").textContent = message;
  show("error");
}

// --- host permissions ----------------------------------------------------
//
// chrome.cookies.getAll({url}) and chrome.scripting.executeScript({tabId})
// require explicit host permission for the target URL — activeTab does not
// satisfy the cookies API. Manifest declares <all_urls> as optional so we
// ask for it per-origin, on the user gesture from the popup, only when the
// flow needs it. Permission persists across sessions.

function originPatternFor(url) {
  try {
    const u = new URL(url);
    if (!/^https?:$/.test(u.protocol)) return null;
    return `${u.protocol}//${u.hostname}/*`;
  } catch (_) {
    return null;
  }
}

function hostFromUrl(url) {
  try { return new URL(url).hostname; } catch (_) { return url; }
}

async function ensureHostPermission(urls) {
  const origins = [...new Set(urls.map(originPatternFor).filter(Boolean))];
  if (origins.length === 0) {
    return { ok: false, reason: "unsupported_scheme" };
  }
  const has = await chrome.permissions.contains({ origins });
  if (has) return { ok: true, origins };
  let granted = false;
  try {
    granted = await chrome.permissions.request({ origins });
  } catch (e) {
    return { ok: false, reason: "permission_request_failed", origins, error: e?.message || String(e) };
  }
  return granted ? { ok: true, origins } : { ok: false, reason: "permission_denied", origins };
}

function updateCaptureModeUI(mode) {
  document.querySelectorAll("#capture-mode-group .pill").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.dataset.mode === mode));
  });
}

function showPermissionError(perm, urls) {
  if (perm.reason === "unsupported_scheme") {
    showError(t("unsupportedSchemeBody"));
    return;
  }
  // permission_denied (or request_failed): build a per-host message.
  const host = urls.length === 1 ? hostFromUrl(urls[0]) : `${urls.length} sites`;
  $("error-message").textContent =
    `${t("permissionRequiredTitle", [host])} — ${t("permissionRequiredBody", [host])}`;
  show("error");
}

// --- init ----------------------------------------------------------------

async function init() {
  setStatus("loading", "statusChecking");
  const pairingResp = await send("get-pairing");
  const pairing = pairingResp.pairing || {};

  if (!pairing.token) {
    setStatus("warn", "statusNotPaired");
    show("unpaired");
    return;
  }

  const identity = await send("check-identity");
  if (!identity?.ok) {
    setStatus("error", identity?.reason === "fingerprint_mismatch" ? "statusServerChanged" : "statusOffline");
    show("error");
    $("error-message").textContent =
      identity?.reason === "fingerprint_mismatch"
        ? t("errorServerFingerprintMismatch")
        : t("errorServerUnreachable", [pairing.serverUrl || "localhost:8080"]);
    return;
  }

  setStatus("ok", "statusConnected");
  $("label-chip").textContent = pairing.label ? t("popupPairedAsLabel", [pairing.label]) : "";
  $("rotate-token").hidden = !pairing.tokenId;

  const casesResp = await send("list-cases");
  if (!casesResp?.ok) {
    showError(t("errorCasesLoadFailed", [casesResp?.error || "unknown"]));
    return;
  }
  const cases = casesResp.cases || [];
  const select = $("case-select");
  select.innerHTML = "";
  for (const c of cases) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.name;
    select.appendChild(opt);
  }

  const activeResp = await send("get-active-case");
  if (activeResp?.caseId && cases.find((c) => c.id === activeResp.caseId)) {
    select.value = activeResp.caseId;
  }
  select.addEventListener("change", () => {
    send("set-active-case", { caseId: parseInt(select.value, 10) });
  });

  // Hydrate the settings checkboxes from chrome.storage.
  const settingsResp = await send("get-settings");
  if (settingsResp?.ok) {
    const s = settingsResp.settings || {};
    $("live-capture").checked = !!s.live_capture;
    $("block-ads").checked = s.block_ads !== false;  // default ON
    $("real-har").checked = !!s.real_har;
    $("ephemeral-cookies").checked = s.cookie_persistence === "ephemeral";
    updateCaptureModeUI(s.capture_mode || null);
  }

  show("paired");
}

// --- actions -------------------------------------------------------------

async function getActiveTabUrl() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab?.url || null;
}

function selectedCaseId() {
  const v = $("case-select").value;
  return v ? parseInt(v, 10) : null;
}

function liveCaptureEnabled() {
  return $("live-capture").checked;
}

function currentCaptureMode() {
  const active = document.querySelector("#capture-mode-group .pill[aria-pressed='true']");
  return active ? active.dataset.mode : null;
}

async function sendActiveTab() {
  const url = await getActiveTabUrl();
  if (!url) return showError(t("errorNoActiveTab"));
  const caseId = selectedCaseId();
  if (!caseId) return showError(t("errorNoCaseSelected"));
  const perm = await ensureHostPermission([url]);
  if (!perm.ok) return showPermissionError(perm, [url]);
  const resp = await send("submit-capture", {
    caseId,
    urls: [url],
    includeLiveCapture: liveCaptureEnabled(),
    captureMode: currentCaptureMode(),
  });
  renderResult(resp);
}

async function sendList() {
  const text = $("list-urls").value || "";
  const urls = text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 25);
  if (urls.length === 0) return showError(t("errorNoUrlsProvided"));
  const caseId = selectedCaseId();
  if (!caseId) return showError(t("errorNoCaseSelected"));
  const perm = await ensureHostPermission(urls);
  if (!perm.ok) return showPermissionError(perm, urls);
  const resp = await send("submit-capture", {
    caseId,
    urls,
    includeLiveCapture: liveCaptureEnabled(),
    captureMode: currentCaptureMode(),
  });
  renderResult(resp);
}

async function syncCookies() {
  const url = await getActiveTabUrl();
  if (!url) return showError(t("errorNoActiveTab"));
  const caseId = selectedCaseId();
  if (!caseId) return showError(t("errorNoCaseSelected"));
  const perm = await ensureHostPermission([url]);
  if (!perm.ok) return showPermissionError(perm, [url]);
  const resp = await send("sync-cookies", { caseId, url });
  if (!resp?.ok) return showError(t("errorSyncFailed", [resp?.error || "unknown"]));
  const domains = (resp.summary?.domains || []).map((d) => d.domain);
  $("result-warnings").hidden = true;
  $("result-jobs").innerHTML = "";
  if (domains.length === 0) {
    // Permission was granted but the page had no cookies — render as a soft
    // warning, not silent success, so the investigator doesn't think the
    // capture is authenticated when it isn't.
    $("result-title").textContent = t("noCookiesFoundTitle");
    const li = document.createElement("li");
    const hint = document.createElement("div");
    hint.textContent = t("noCookiesFoundBody");
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = url;
    li.append(hint, meta);
    $("result-jobs").appendChild(li);
  } else {
    $("result-title").textContent = t("resultCookiesSyncedTitle");
    const li = document.createElement("li");
    const div = document.createElement("div");
    div.textContent = domains.join(", ");
    li.appendChild(div);
    $("result-jobs").appendChild(li);
  }
  show("result");
}

async function rotateToken() {
  const resp = await send("rotate-token");
  if (!resp?.ok) {
    showError(t("errorRotateTokenFailed", [resp?.error || "unknown"]));
    return;
  }
  $("result-title").textContent = t("resultTokenRotatedTitle");
  $("result-warnings").hidden = true;
  const li = document.createElement("li");
  const div = document.createElement("div");
  div.className = "muted";
  div.textContent = t("resultNewTokenIdLabel", [resp.token_id]);
  li.appendChild(div);
  $("result-jobs").innerHTML = "";
  $("result-jobs").appendChild(li);
  show("result");
}

function renderResult(resp) {
  if (!resp?.ok) return showError(resp?.error || t("errorSubmissionFailed"));
  const jobCount = resp.jobs?.length || 0;
  $("result-title").textContent = tPlural("sentCount", jobCount);

  // Render any partial-capture warnings prominently — never silent success.
  const wul = $("result-warnings");
  wul.innerHTML = "";
  const warnings = [
    ...(resp.capture_warnings || []),
    ...((resp.skipped_urls || []).map((u) => t("warningCookiesSkipped", [u]))),
  ];
  if (warnings.length) {
    for (const w of warnings) {
      const li = document.createElement("li");
      li.textContent = w;
      wul.appendChild(li);
    }
    wul.hidden = false;
  } else {
    wul.hidden = true;
  }

  const ul = $("result-jobs");
  ul.innerHTML = "";
  for (const job of resp.jobs || []) {
    const li = document.createElement("li");
    const url = document.createElement("div");
    url.className = "job-url";
    url.textContent = job.url;
    const status = document.createElement("div");
    status.className = "job-status";
    status.textContent = job.status;
    li.append(url, status);
    ul.appendChild(li);
  }
  show("result");
}

// --- wiring --------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  i18nApply();

  $("open-pair-page").addEventListener("click", () => {
    chrome.tabs.create({ url: chrome.runtime.getURL("pair/pair.html") });
  });

  $("send-tab").addEventListener("click", sendActiveTab);
  $("send-list-toggle").addEventListener("click", () => {
    $("list-form").hidden = !$("list-form").hidden;
  });
  $("send-list").addEventListener("click", sendList);
  $("sync-cookies").addEventListener("click", syncCookies);
  $("rotate-token").addEventListener("click", rotateToken);

  // Settings toggles persist to chrome.storage via the background.
  $("live-capture").addEventListener("change", () => {
    send("set-settings", { live_capture: $("live-capture").checked });
  });
  $("block-ads").addEventListener("change", () => {
    send("set-settings", { block_ads: $("block-ads").checked });
  });
  $("real-har").addEventListener("change", async () => {
    if ($("real-har").checked && chrome.permissions) {
      // chrome.debugger needs the optional permission granted at runtime.
      const granted = await chrome.permissions.request({ permissions: ["debugger"] });
      if (!granted) {
        $("real-har").checked = false;
        return;
      }
    }
    send("set-settings", { real_har: $("real-har").checked });
  });
  $("ephemeral-cookies").addEventListener("change", () => {
    send("set-settings", {
      cookie_persistence: $("ephemeral-cookies").checked ? "ephemeral" : "case",
    });
  });

  document.querySelectorAll("#capture-mode-group .pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      // Toggle off if already active, otherwise activate the clicked mode.
      const newMode = btn.getAttribute("aria-pressed") === "true"
        ? null
        : btn.dataset.mode;
      send("set-settings", { capture_mode: newMode });
      updateCaptureModeUI(newMode);
    });
  });

  $("manage").addEventListener("click", () => {
    chrome.tabs.create({ url: chrome.runtime.getURL("pair/pair.html") });
  });

  $("back-to-paired").addEventListener("click", () => show("paired"));
  $("error-back").addEventListener("click", () => show("paired"));
  $("retry").addEventListener("click", init);

  init().catch((e) => showError(e?.message || String(e)));
});
