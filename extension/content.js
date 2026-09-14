// Renders the verdict panel and drives scoring for the current page.
//
// Scoring is not automatic on every page. It runs when the page looks like a
// single job posting and auto-scoring is on, or when you ask for it from the
// popup. Results are cached per URL by the server, so revisiting is free.

(function () {
  // The popup injects this script when a tab was already open before the
  // extension loaded, so it can run on top of a copy that is already here.
  // Without this guard the second run registers a second message listener and
  // every request gets handled twice.
  if (window.__resumeAgentContentLoaded) return;
  window.__resumeAgentContentLoaded = true;

  console.log("[resume-agent] v5 (autofill) ready on", location.host);

  // Styles live here rather than in a separate file so the panel can never
  // render half-loaded. They are injected into the shadow root below.
  const PANEL_CSS = `
:host { all: initial; display: block; }

.wrap {
  box-sizing: border-box;
  width: 350px;
  max-height: 84vh;
  overflow-y: auto;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 13px;
  line-height: 1.45;
  color: #1a1d21;
  background: #ffffff;
  border: 1px solid #d5d9e0;
  border-radius: 10px;
  box-shadow: 0 8px 28px rgba(16, 24, 40, 0.18);
}
.wrap * { box-sizing: border-box; }

.head {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 14px; color: #ffffff;
}
.apply { background: #1a7f37; }
.maybe { background: #b26a00; }
.skip { background: #5c6470; }
.no { background: #b42318; }
.loading { background: #3b4451; }

.verdict { font-size: 17px; font-weight: 700; letter-spacing: 0.04em; }
.score { margin-left: auto; font-size: 14px; font-weight: 600; }
.close {
  background: transparent; border: 0; color: #ffffff; font-size: 19px;
  line-height: 1; cursor: pointer; padding: 0 2px; opacity: 0.85;
}
.close:hover { opacity: 1; }

.body { padding: 12px 14px 14px; }
.headline { font-weight: 600; margin: 0 0 3px; }
.meta { color: #5c6470; margin: 0 0 10px; }

.warn {
  background: #fdf0ef; border: 1px solid #f3c9c5; color: #8c1d18;
  border-radius: 6px; padding: 8px 10px; margin: 10px 0;
}
.quote { margin-top: 6px; font-style: italic; color: #6b2b26; font-size: 12px; }

/* Collapsible sections. Native details/summary, so keyboard and screen
   readers get the disclosure behaviour without any script. */
.sect { margin-top: 10px; border-top: 1px solid #eceef1; padding-top: 9px; }
.sect > summary {
  cursor: pointer; list-style: none; display: flex; align-items: center; gap: 6px;
  font-weight: 700; font-size: 11px; letter-spacing: 0.06em;
  text-transform: uppercase; color: #5c6470; user-select: none;
}
.sect > summary::-webkit-details-marker { display: none; }
.sect > summary::after { content: "+"; margin-left: auto; font-size: 14px; line-height: 1; }
.sect[open] > summary::after { content: "\\2212"; }
.sect > summary:hover { color: #1a1d21; }
.count { font-weight: 600; text-transform: none; letter-spacing: 0; opacity: 0.8; }

.list { margin: 7px 0 0; padding: 0 0 0 17px; }
.list li { margin: 0 0 4px; }

.actions { margin-top: 14px; display: flex; flex-direction: column; gap: 8px; }
.btn {
  display: block; width: 100%; padding: 9px 12px; border-radius: 6px;
  border: 1px solid #d5d9e0; background: #f6f7f9; color: #1a1d21;
  font-family: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
}
.btn:hover:enabled { border-color: #adb4bf; }
.btn-primary { background: #1a1d21; border-color: #1a1d21; color: #ffffff; }
.btn:disabled { opacity: 0.6; cursor: default; }

.noaction {
  margin-top: 14px; padding: 9px 11px; border-radius: 6px;
  background: #f4f5f7; color: #5c6470; font-size: 12px;
}
.override {
  display: inline-block; margin-top: 7px; background: none; border: 0; padding: 0;
  color: #5c6470; font-family: inherit; font-size: 12px;
  text-decoration: underline; cursor: pointer;
}

.msg { margin-top: 10px; }
.msg-subject {
  font-size: 12px; font-weight: 600; margin-bottom: 6px;
  padding: 6px 8px; background: #f4f5f7; border-radius: 5px; word-break: break-word;
}
.msg-body {
  display: block; width: 100%; min-height: 168px; resize: vertical;
  padding: 8px 9px; border: 1px solid #d5d9e0; border-radius: 6px;
  font-family: inherit; font-size: 12px; line-height: 1.5;
  color: #1a1d21; background: #ffffff;
}
.hint { margin-top: 6px; font-size: 11px; color: #7a828e; }

.saved { margin-top: 8px; font-size: 12px; color: #1a7f37; word-break: break-all; }
.foot {
  margin-top: 12px; padding-top: 10px; border-top: 1px solid #eceef1;
  font-size: 11px; color: #7a828e;
}

.fill { margin-top: 10px; }
.why { color: #7a828e; }

@media (prefers-color-scheme: dark) {
  .wrap { color: #e6e8eb; background: #1b1f24; border-color: #333a42; }
  .meta, .count { color: #9aa4b2; }
  .sect { border-top-color: #2c333a; }
  .sect > summary { color: #9aa4b2; }
  .sect > summary:hover { color: #e6e8eb; }
  .btn { background: #262c33; border-color: #3a424b; color: #e6e8eb; }
  .btn:hover:enabled { border-color: #55606c; }
  .btn-primary { background: #e6e8eb; border-color: #e6e8eb; color: #1b1f24; }
  .warn { background: #3a1d1b; border-color: #6b2b26; color: #f5c4c0; }
  .quote { color: #e0a8a3; }
  .noaction { background: #262c33; color: #9aa4b2; }
  .override { color: #9aa4b2; }
  .msg-subject { background: #262c33; }
  .msg-body { background: #14181c; border-color: #3a424b; color: #e6e8eb; }
  .hint, .foot, .why { color: #8b939f; }
  .foot { border-top-color: #2c333a; }
  .saved { color: #4ac26b; }
}
`;

  const HOST_ID = "resume-agent-host";
  let lastResult = null;
  let inFlight = false;

  const VERDICT_CLASS = { APPLY: "apply", MAYBE: "maybe", SKIP: "skip", NO: "no" };

  // A tailored resume and a cold message each cost a model call. Offering them
  // on a posting the scorer just called a weak match is asking you to spend
  // that on something you were told not to apply to.
  const WORTH_EFFORT = new Set(["APPLY", "MAYBE"]);

  // --- shell ----------------------------------------------------------------
  function shadow() {
    document.getElementById(HOST_ID)?.remove();

    const host = document.createElement("div");
    host.id = HOST_ID;
    // Set inline so a page stylesheet cannot move the panel.
    host.style.cssText =
      "all:initial;position:fixed;top:16px;right:16px;z-index:2147483647;";

    const root = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = PANEL_CSS;
    root.appendChild(style);

    const wrap = document.createElement("div");
    wrap.className = "wrap";
    root.appendChild(wrap);

    document.body.appendChild(host);
    return wrap;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function closeButton() {
    const btn = el("button", "close", "×");
    btn.title = "Dismiss";
    btn.addEventListener("click", () => document.getElementById(HOST_ID)?.remove());
    return btn;
  }

  function banner(cls, label, score) {
    const head = el("div", `head ${cls}`);
    head.appendChild(el("span", "verdict", label));
    if (score) head.appendChild(el("span", "score", score));
    head.appendChild(closeButton());
    return head;
  }

  function renderLoading() {
    const wrap = shadow();
    wrap.appendChild(banner("loading", "Scoring..."));
  }

  function renderError(message) {
    const wrap = shadow();
    wrap.appendChild(banner("skip", "Error"));
    const body = el("div", "body");
    body.appendChild(el("div", "headline", message));
    wrap.appendChild(body);
  }

  // --- collapsible section --------------------------------------------------
  function collapsible(label, items) {
    const details = el("details", "sect");
    const summary = el("summary");
    summary.appendChild(document.createTextNode(label));
    summary.appendChild(el("span", "count", `(${items.length})`));
    details.appendChild(summary);

    const list = el("ul", "list");
    items.forEach((t) => list.appendChild(el("li", null, t)));
    details.appendChild(list);
    return details;
  }

  // --- result ---------------------------------------------------------------
  function renderResult(data) {
    lastResult = data;
    const wrap = shadow();
    wrap.appendChild(banner(VERDICT_CLASS[data.verdict] || "skip", data.verdict, `${data.score}/100`));

    const body = el("div", "body");
    body.appendChild(el("div", "headline", data.headline));

    const meta = [data.company, data.title].filter(Boolean).join(" · ");
    if (meta) body.appendChild(el("div", "meta", meta));

    if (data.sponsorship_blocked) {
      const warn = el("div", "warn");
      warn.appendChild(el("strong", null, "No sponsorship. "));
      warn.appendChild(document.createTextNode(
        data.sponsorship_source === "job description text"
          ? "The posting says so directly."
          : "Read from the posting."
      ));
      if (data.sponsorship_evidence) {
        warn.appendChild(el("div", "quote", `"${data.sponsorship_evidence}"`));
      }
      body.appendChild(warn);
    }

    if (data.strengths?.length) body.appendChild(collapsible("Strengths", data.strengths));
    if (data.gaps?.length) body.appendChild(collapsible("Gaps", data.gaps));

    body.appendChild(actionsFor(data));
    body.appendChild(el("div", "foot", "Autofill submits only when every question is answered and no CAPTCHA guards the form."));
    wrap.appendChild(body);
  }

  function actionsFor(data) {
    if (WORTH_EFFORT.has(data.verdict)) return actionButtons();

    const box = el("div", "noaction");
    if (data.verdict === "NO") {
      box.appendChild(document.createTextNode(
        "Nothing generated. This posting rules out sponsorship, so the score does not matter."
      ));
      return box;
    }

    box.appendChild(document.createTextNode(
      `Below the ${data.thresholds?.maybe ?? 50} point bar, so nothing was generated.`
    ));
    // An escape hatch, deliberately quiet. The scorer is not always right.
    const override = el("button", "override", "Draft anyway");
    override.addEventListener("click", () => box.replaceChildren(actionButtons()));
    box.appendChild(document.createElement("br"));
    box.appendChild(override);
    return box;
  }

  function actionButtons() {
    const actions = el("div", "actions");

    const resumeBtn = el("button", "btn btn-primary", "Generate tailored resume");
    resumeBtn.addEventListener("click", () => makeResume(resumeBtn, actions));
    actions.appendChild(resumeBtn);

    const msgBtn = el("button", "btn", "Draft cold message");
    msgBtn.addEventListener("click", () => makeMessage(msgBtn, actions));
    actions.appendChild(msgBtn);

    const fillBtn = el("button", "btn", "Autofill application");
    fillBtn.addEventListener("click", () => makeAutofill(fillBtn, actions));
    actions.appendChild(fillBtn);

    return actions;
  }

  // --- actions --------------------------------------------------------------
  function currentText() {
    const posting = window.__resumeAgentExtract.extract();
    return posting ? posting.text : "";
  }

  function makeResume(button, container) {
    if (!lastResult) return;
    button.disabled = true;
    button.textContent = "Tailoring and compiling...";

    chrome.runtime.sendMessage(
      {
        type: "generate-resume",
        payload: {
          url: lastResult.url,
          title: lastResult.title,
          company: lastResult.company,
          text: currentText(),
          score: lastResult.score,
          strengths: lastResult.strengths,
          gaps: lastResult.gaps,
        },
      },
      (reply) => {
        button.disabled = false;
        if (!reply || !reply.ok) {
          button.textContent = "Failed, click to retry";
          container.appendChild(el("div", "warn", (reply && reply.error) || "Resume generation failed."));
          return;
        }
        button.textContent = "Resume saved";
        container.appendChild(el("div", "saved", reply.pdf_path));
      }
    );
  }

  function makeMessage(button, container) {
    if (!lastResult) return;
    button.disabled = true;
    button.textContent = "Drafting...";

    chrome.runtime.sendMessage(
      {
        type: "draft-message",
        payload: {
          title: lastResult.title,
          company: lastResult.company,
          text: currentText(),
        },
      },
      (reply) => {
        button.disabled = false;
        button.textContent = "Redraft cold message";
        if (!reply || !reply.ok) {
          container.appendChild(el("div", "warn", (reply && reply.error) || "Could not draft a message."));
          return;
        }
        container.querySelector(".msg")?.remove();
        container.appendChild(messageBlock(reply));
      }
    );
  }

  function messageBlock(reply) {
    const box = el("div", "msg");
    if (reply.subject) box.appendChild(el("div", "msg-subject", reply.subject));

    // A textarea rather than static text: the draft is a starting point and
    // you will want to edit it before sending.
    const area = document.createElement("textarea");
    area.className = "msg-body";
    area.value = reply.body || "";
    box.appendChild(area);

    const copy = el("button", "btn", "Copy message");
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(area.value);
        copy.textContent = "Copied";
      } catch (e) {
        // Clipboard access can be denied by page permissions policy.
        area.select();
        copy.textContent = "Press Cmd-C to copy";
      }
      setTimeout(() => { copy.textContent = "Copy message"; }, 2000);
    });
    box.appendChild(copy);

    box.appendChild(el("div", "hint", "Replace [name] and attach your resume before sending. Nothing is sent for you."));
    return box;
  }

  // --- autofill -------------------------------------------------------------
  function makeAutofill(button, container) {
    button.disabled = true;
    button.textContent = "Loading your profile...";
    chrome.runtime.sendMessage({ type: "get-profile" }, (prof) => {
      if (!prof || !prof.ok) {
        button.disabled = false;
        button.textContent = "Autofill application";
        container.appendChild(el("div", "warn", (prof && prof.error) || "Could not load your profile."));
        return;
      }
      chrome.runtime.sendMessage({ type: "get-resume-file" }, async (res) => {
        const resume = res && res.ok ? res : null;
        button.textContent = "Filling...";
        let out;
        try {
          out = await window.__resumeAgentAutofill.run(prof.profile, resume);
        } catch (e) {
          out = { error: `Autofill failed: ${e.message || e}` };
        }
        button.disabled = false;
        button.textContent = "Autofill again";
        container.querySelector(".fill")?.remove();
        container.appendChild(fillReport(out, resume ? "" : (res && res.error) || ""));
      });
    });
  }

  function fillReport(out, resumeProblem) {
    const box = el("div", "fill");
    if (out.error) {
      box.appendChild(el("div", "warn", out.error));
      return box;
    }

    const kept = out.kept ? `, kept ${out.kept} you had already filled` : "";
    box.appendChild(el("div", "headline", `Filled ${out.filled}${kept}.`));
    if (resumeProblem) box.appendChild(el("div", "hint", resumeProblem));

    if (out.needs.length) {
      box.appendChild(el("div", "label", "Needs you"));
      const list = el("ul", "list");
      out.needs.slice(0, 12).forEach((n) => {
        const item = el("li", null, n.label);
        item.appendChild(el("span", "why", ` · ${n.reason}`));
        list.appendChild(item);
      });
      if (out.needs.length > 12) {
        list.appendChild(el("li", null, `and ${out.needs.length - 12} more, outlined on the page`));
      }
      box.appendChild(list);
    }

    if (!out.eligible) {
      box.appendChild(el("div", "hint",
        `Not submitting. ${out.reasons.join(" ")} Filled fields are outlined green, the rest amber.`));
      return box;
    }

    // Everything answered: a short countdown you can cancel, rather than an
    // instant submit you cannot take back.
    let left = 5;
    const line = el("div", "hint", `Everything is answered. Submitting in ${left}s.`);
    const cancel = el("button", "btn", "Cancel submit");
    const timer = setInterval(() => {
      // Closing the panel cancels too.
      if (!line.isConnected) {
        clearInterval(timer);
        return;
      }
      left -= 1;
      if (left > 0) {
        line.textContent = `Everything is answered. Submitting in ${left}s.`;
        return;
      }
      clearInterval(timer);
      cancel.remove();
      const sent = window.__resumeAgentAutofill.submitNow(out.submit);
      line.textContent = sent.ok
        ? "Submitted. Check the page for the confirmation."
        : `Did not submit: ${sent.reason}.`;
    }, 1000);
    cancel.addEventListener("click", () => {
      clearInterval(timer);
      cancel.remove();
      line.textContent = "Submit cancelled. Review the form and submit it yourself.";
    });
    box.appendChild(line);
    box.appendChild(cancel);
    return box;
  }

  // --- scoring --------------------------------------------------------------
  function score(force) {
    if (inFlight) return;
    const posting = window.__resumeAgentExtract.extract();
    if (!posting) {
      if (force) renderError("No job description found on this page.");
      return;
    }

    inFlight = true;
    renderLoading();
    chrome.runtime.sendMessage({ type: "score", payload: posting }, (reply) => {
      inFlight = false;
      if (!reply) {
        renderError("Could not reach the extension background worker. Reload the page.");
      } else if (reply.unreachable) {
        renderError("Scoring server is not running. Start it with: python3 server/score_server.py");
      } else if (!reply.ok) {
        renderError(reply.error || "Scoring failed.");
      } else {
        renderResult(reply);
      }
    });
  }

  // The popup calls this directly through chrome.scripting rather than sending
  // a message. Messaging a tab whose content script has not loaded fails with
  // "receiving end does not exist"; calling in after injecting cannot, because
  // the injection is what defines this function.
  window.__resumeAgentScoreNow = function () {
    score(true);
  };

  chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
    if (msg && msg.type === "score-now") {
      score(true);
      respond({ started: true });
    }
    return true;
  });

  chrome.storage.sync.get({ autoScore: true }, (settings) => {
    if (settings.autoScore && window.__resumeAgentExtract.looksLikeJobPage()) {
      // Single-page job boards swap content in after load, so let the page
      // settle before reading it.
      setTimeout(() => score(false), 1200);
    }
  });
})();
