const statusEl = document.getElementById("status");
const scoreBtn = document.getElementById("score");

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

// --- scoring -----------------------------------------------------------------
// Chrome only injects content scripts into tabs that load after the extension
// does, so a tab that was already open has nothing listening. Rather than
// message it and handle the failure, inject first and then call in. Injection
// is idempotent: content.js returns early if it is already present.

async function scoreActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  if (!tab || !tab.id) throw new Error("No active tab.");
  if (!/^https?:/i.test(tab.url || "")) {
    throw new Error("Only works on http and https pages.");
  }

  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["extract.js", "content.js"],
  });

  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => {
      if (typeof window.__resumeAgentScoreNow !== "function") return "missing";
      window.__resumeAgentScoreNow();
      return "started";
    },
  });

  if (!result || result.result !== "started") {
    throw new Error("Could not start scoring on this page.");
  }
}

scoreBtn.addEventListener("click", async () => {
  scoreBtn.disabled = true;
  setStatus("Reading the page...");
  try {
    await scoreActiveTab();
    setStatus("Scoring. Check the panel on the page.", "ok");
    setTimeout(() => window.close(), 1000);
  } catch (e) {
    // Chrome blocks injection on its own pages, the web store, and PDFs.
    setStatus(e.message || String(e), "bad");
  } finally {
    scoreBtn.disabled = false;
  }
});
