// Pairing page logic. Calls into the background worker, which is the
// only place that ever holds the raw token.

const $ = (id) => document.getElementById(id);

function send(type, payload) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, payload }, (resp) => resolve(resp || { ok: false, error: "no_response" }));
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
    $("result").textContent =
      `Paired with ${p.serverUrl} (server fingerprint: ${p.fingerprint || "unknown"}). The token is stored locally — change it by re-pairing or unpair below.`;
  }
}

async function pair() {
  const serverUrl = $("server-url").value.trim();
  const token = $("token").value.trim();
  const label = $("label").value.trim() || "Browser extension";
  if (!serverUrl || !token) {
    setResult("error", "Server URL and token are both required.");
    return;
  }
  const resp = await send("pair", { serverUrl, token, label });
  if (!resp?.ok) {
    setResult("error", resp?.error || "Pairing failed.");
    return;
  }
  setResult(
    "ok",
    `Paired. Server fingerprint: ${resp.fingerprint}. You can close this tab and open the popup.`
  );
  $("token").value = ""; // don't leave the raw token in the input
}

function setResult(state, text) {
  const el = $("result");
  el.hidden = false;
  el.className = state === "ok" ? "ok" : "error";
  el.textContent = text;
}

document.addEventListener("DOMContentLoaded", () => {
  refresh();
  $("pair").addEventListener("click", pair);
  $("unpair").addEventListener("click", async () => {
    await send("clear-pairing");
    setResult("ok", "Unpaired. The local token has been removed from the browser.");
  });
});
