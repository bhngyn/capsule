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

function setStatus(state, text) {
  const pill = $("status-pill");
  pill.dataset.state = state;
  pill.querySelector(".status-text").textContent = text;
}

function showError(message) {
  $("error-message").textContent = message;
  show("error");
}

// --- init ----------------------------------------------------------------

async function init() {
  setStatus("loading", "Checking…");
  const pairingResp = await send("get-pairing");
  const pairing = pairingResp.pairing || {};

  if (!pairing.token) {
    setStatus("warn", "Not paired");
    show("unpaired");
    return;
  }

  const identity = await send("check-identity");
  if (!identity?.ok) {
    setStatus("error", identity?.reason === "fingerprint_mismatch" ? "Server changed" : "Offline");
    show("error");
    $("error-message").textContent =
      identity?.reason === "fingerprint_mismatch"
        ? "The Capsule server's signing key has changed. Re-pair the extension."
        : "Capsule isn't reachable at " + (pairing.serverUrl || "localhost:8080") + ". Make sure the app is running.";
    return;
  }

  setStatus("ok", "Connected");
  $("label-chip").textContent = pairing.label ? `paired as “${pairing.label}”` : "";
  $("rotate-token").hidden = !pairing.tokenId;

  const casesResp = await send("list-cases");
  if (!casesResp?.ok) {
    showError("Failed to load cases: " + (casesResp?.error || "unknown"));
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

async function sendActiveTab() {
  const url = await getActiveTabUrl();
  if (!url) return showError("No active tab.");
  const caseId = selectedCaseId();
  if (!caseId) return showError("Pick a case first.");
  const resp = await send("submit-capture", {
    caseId,
    urls: [url],
    includeLiveCapture: liveCaptureEnabled(),
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
  if (urls.length === 0) return showError("Paste at least one URL.");
  const caseId = selectedCaseId();
  if (!caseId) return showError("Pick a case first.");
  const resp = await send("submit-capture", {
    caseId,
    urls,
    includeLiveCapture: liveCaptureEnabled(),
  });
  renderResult(resp);
}

async function syncCookies() {
  const url = await getActiveTabUrl();
  if (!url) return showError("No active tab.");
  const caseId = selectedCaseId();
  if (!caseId) return showError("Pick a case first.");
  const resp = await send("sync-cookies", { caseId, url });
  if (!resp?.ok) return showError("Sync failed: " + (resp?.error || "unknown"));
  $("result-title").textContent = "Cookies synced";
  $("result-warnings").hidden = true;
  $("result-jobs").innerHTML = `<li><div>${(resp.summary?.domains || []).map((d) => d.domain).join(", ") || "no domains"}</div></li>`;
  show("result");
}

async function rotateToken() {
  const resp = await send("rotate-token");
  if (!resp?.ok) {
    showError("Could not rotate token: " + (resp?.error || "unknown"));
    return;
  }
  $("result-title").textContent = "Token rotated";
  $("result-warnings").hidden = true;
  $("result-jobs").innerHTML = `<li><div class="muted">New token id: ${resp.token_id}</div></li>`;
  show("result");
}

function renderResult(resp) {
  if (!resp?.ok) return showError(resp?.error || "Submission failed");
  const jobCount = resp.jobs?.length || 0;
  $("result-title").textContent =
    `Sent ${jobCount} ${jobCount === 1 ? "capture" : "captures"}`;

  // Render any partial-capture warnings prominently — never silent success.
  const wul = $("result-warnings");
  wul.innerHTML = "";
  const warnings = [
    ...(resp.capture_warnings || []),
    ...((resp.skipped_urls || []).map((u) => `cookies skipped for: ${u}`)),
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

  $("manage").addEventListener("click", () => {
    chrome.tabs.create({ url: chrome.runtime.getURL("pair/pair.html") });
  });

  $("back-to-paired").addEventListener("click", () => show("paired"));
  $("error-back").addEventListener("click", () => show("paired"));
  $("retry").addEventListener("click", init);

  init().catch((e) => showError(e?.message || String(e)));
});
