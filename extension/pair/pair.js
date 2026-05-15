// Pairing page logic. Calls into the background worker, which is the
// only place that ever holds the raw token.

const $ = (id) => document.getElementById(id);

function send(type, payload) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, payload }, (resp) => resolve(resp || { ok: false, error: "no_response" }));
  });
}

function t(key, substitutions) {
  try {
    return chrome.i18n.getMessage(key, substitutions) || key;
  } catch (_) {
    return key;
  }
}

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

async function refresh() {
  const resp = await send("get-pairing");
  const p = resp?.pairing || {};
  if (p.serverUrl) $("server-url").value = p.serverUrl;
  if (p.label) $("label").value = p.label;
  if (p.token) {
    $("result").hidden = false;
    $("result").className = "ok";
    $("result").textContent = t("pairAlreadyPairedMessage", [
      p.serverUrl || "",
      p.fingerprint || "unknown",
    ]);
  }
}

async function deriveTokenId(raw) {
  // Mirrors backend: token_id = first 12 hex of sha256(raw_token).
  // Computing client-side avoids round-tripping the id alongside the
  // raw token through the user's clipboard.
  const enc = new TextEncoder();
  const buf = await crypto.subtle.digest("SHA-256", enc.encode(raw));
  const bytes = Array.from(new Uint8Array(buf));
  const hex = bytes.map((b) => b.toString(16).padStart(2, "0")).join("");
  return hex.slice(0, 12);
}

async function pair() {
  const serverUrl = $("server-url").value.trim();
  const token = $("token").value.trim();
  const label = $("label").value.trim() || t("pairDefaultDeviceLabel");
  if (!serverUrl || !token) {
    setResult("error", t("errorPairMissingServerOrToken"));
    return;
  }
  let tokenId = "";
  try {
    tokenId = await deriveTokenId(token);
  } catch (_) {
    // crypto.subtle missing — proceed with empty id, rotation just won't work.
  }
  const resp = await send("pair", { serverUrl, token, tokenId, label });
  if (!resp?.ok) {
    setResult("error", resp?.error || t("errorPairFailed"));
    return;
  }
  setResult("ok", t("pairSuccessMessage", [resp.fingerprint]));
  $("token").value = ""; // don't leave the raw token in the input
}

function setResult(state, text) {
  const el = $("result");
  el.hidden = false;
  el.className = state === "ok" ? "ok" : "error";
  el.textContent = text;
}

document.addEventListener("DOMContentLoaded", () => {
  i18nApply();
  refresh();
  $("pair").addEventListener("click", pair);
  $("unpair").addEventListener("click", async () => {
    await send("clear-pairing");
    setResult("ok", t("pairUnpairedMessage"));
  });
});
