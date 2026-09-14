const statusEl = document.getElementById("status");
const scoreBtn = document.getElementById("score");
const fillBtn = document.getElementById("fill");

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

// --- server reachability -----------------------------------------------------
fetch("http://127.0.0.1:8765/health")
  .then((r) => r.json())
  .then(() => setStatus("Server running", "ok"))
  .catch(() => {
    statusEl.className = "bad";
    statusEl.innerHTML = 'Server not running. Start it with <code>python3 server/score_server.py</code>';
  });

// --- auto-score toggle -------------------------------------------------------
chrome.storage.sync.get({ autoScore: true }, ({ autoScore }) => {
  document.getElementById("auto").checked = autoScore;
});
document.getElementById("auto").addEventListener("change", (e) => {
  chrome.storage.sync.set({ autoScore: e.target.checked });
});

// --- running in the page -----------------------------------------------------
// Chrome only injects content scripts into tabs that load after the extension
// does, so a tab that was already open has nothing listening. Rather than
// message it and handle the failure, inject first and then call in. Injection
// is idempotent: each script returns early if it is already present.

async function activeWebTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) throw new Error("No active tab.");
  if (!/^https?:/i.test(tab.url || "")) throw new Error("Only works on http and https pages.");
  return tab;
}

async function callInPage(entryPoint) {
  const tab = await activeWebTab();
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["extract.js", "autofill.js", "content.js"],
  });
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    args: [entryPoint],
    func: (name) => {
      if (typeof window[name] !== "function") return "missing";
      window[name]();
      return "started";
    },
  });
  if (!result || result.result !== "started") {
    throw new Error("Could not start on this page. Reload the tab and try again.");
  }
}

function wire(button, entryPoint, working, started) {
  if (!button) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    setStatus(working);
    try {
      await callInPage(entryPoint);
      setStatus(started, "ok");
      setTimeout(() => window.close(), 1000);
    } catch (e) {
      // Chrome blocks injection on its own pages, the web store, and PDFs.
      setStatus(e.message || String(e), "bad");
    } finally {
      button.disabled = false;
    }
  });
}

wire(scoreBtn, "__resumeAgentScoreNow", "Reading the page...", "Scoring. Check the panel on the page.");
// Autofill needs no score: it runs on whatever application form is in front of you.
wire(fillBtn, "__resumeAgentAutofillNow", "Loading your profile...", "Filling. Check the panel on the page.");
