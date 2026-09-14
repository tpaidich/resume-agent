// Fills a job application form from config/profile.yaml, which the local
// server serves to this extension and nothing else.
//
// It submits on its own only when nothing on the form needs a person: no
// free-response question, no question it does not recognize, no agreement to
// accept, no CAPTCHA, and a submit button to press. Otherwise it fills what it
// can, outlines everything, and leaves submitting to you.
//
// It never touches a CAPTCHA and never ticks an agreement for you.

(function () {
  if (window.__resumeAgentAutofill) return;

  const FILLED = "#1a7f37";
  const NEEDS = "#d97706";
  const SKIP = "skip";

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const low = (s) => norm(s).toLowerCase();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // --- reading the form -----------------------------------------------------

  function visible(el) {
    if (!el || !el.isConnected) return false;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  }

  // The visible heading for a field, found by climbing to the container that
  // holds it. Skips the control's own label: on Ashby a radio button's "Male"
  // label sits closer to the input than the "Gender" heading does, and taking
  // the nearest label would read every option as its own question.
  function nearestHeading(el) {
    const own = new Set(el.labels ? [...el.labels] : []);
    let node = el.parentElement;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      const heading = [...node.querySelectorAll(
        ":scope > label, :scope > legend, :scope > [class*='heading'], :scope > [class*='question'], :scope > [class*='title']"
      )].find((h) => !own.has(h) && !h.contains(el) && norm(h.innerText).length > 2);
      if (heading) return norm(heading.innerText);
    }
    return "";
  }

  function labelOf(el) {
    if (el.labels && el.labels.length) {
      return norm([...el.labels].map((l) => l.innerText).join(" "));
    }
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      return norm(by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "));
    }
    // Placeholder text last: "Start typing..." describes the box, not the question.
    return norm(el.getAttribute("aria-label") || nearestHeading(el) || el.placeholder || "");
  }

  // The question a radio button belongs to is the group's heading, not the
  // option's own label: "Female" is an answer, "Gender" is the question.
  function questionOf(el) {
    const legend = el.closest("fieldset")?.querySelector(":scope > legend");
    if (legend && norm(legend.innerText)) return norm(legend.innerText);
    return nearestHeading(el) || labelOf(el);
  }

  const isRequired = (el) => !!(el.required || el.getAttribute("aria-required") === "true");

  function findSubmit() {
    const buttons = [...document.querySelectorAll("button, input[type=submit]")];
    const text = (b) => norm(b.innerText || b.value);
    return (
      buttons.find((b) => /submit( your)? application/i.test(text(b))) ||
      buttons.find((b) => b.type === "submit" && /submit|apply/i.test(text(b))) ||
      null
    );
  }

  function captchaOnPage() {
    const found = document.querySelector([
      'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]', 'iframe[src*="challenges.cloudflare.com"]',
      ".g-recaptcha", ".h-captcha", ".cf-turnstile", "[data-sitekey]",
      'textarea[name="g-recaptcha-response"]', 'textarea[name="h-captcha-response"]',
    ].join(","));
    return !!found || [...document.scripts].some((s) => /recaptcha|hcaptcha|turnstile/i.test(s.src || ""));
  }

  // Whether the posting says it is in the US, from the structured data boards
  // publish. "Authorized to work in the country where the job is located"
  // cannot be answered without knowing the country.
  function jobInUS() {
    for (const block of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data;
      try { data = JSON.parse(block.textContent); } catch (e) { continue; }
      const nodes = Array.isArray(data) ? data : [data, ...((data && data["@graph"]) || [])];
      for (const node of nodes) {
        for (const loc of [].concat((node && node.jobLocation) || [])) {
          const country = low(loc?.address?.addressCountry?.name || loc?.address?.addressCountry || "");
          if (["us", "usa", "united states", "united states of america"].includes(country)) return true;
        }
      }
    }
    return false;
  }

  function collect(scope) {
    const questions = [];
    const seenGroups = new Set();
    for (const el of scope.querySelectorAll("input, textarea, select")) {
      const type = (el.type || "").toLowerCase();
      if (["hidden", "submit", "button", "reset", "image", "search"].includes(type)) continue;
      // CAPTCHA plumbing and the shadow inputs some select widgets add for validation.
      if (/captcha/i.test(el.name || "") || /requiredInput/.test(String(el.className))) continue;

      if (type === "radio") {
        const key = el.name || questionOf(el);
        if (seenGroups.has(key)) continue;
        seenGroups.add(key);
        const group = el.name
          ? [...scope.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)]
          : [el];
        questions.push({ kind: "radio", el, group, label: questionOf(el), required: group.some(isRequired) });
      } else if (type === "file") {
        questions.push({ kind: "file", el, label: labelOf(el), required: isRequired(el) });
      } else if (type === "checkbox") {
        questions.push({ kind: "checkbox", el, label: labelOf(el) || questionOf(el), required: isRequired(el) });
      } else if (visible(el)) {
        const combo =
          el.getAttribute("role") === "combobox" ||
          /select__input/.test(String(el.className)) ||
          el.getAttribute("aria-autocomplete") === "list";
        const kind = el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select" : combo ? "combobox" : "text";
        questions.push({ kind, el, label: labelOf(el), required: isRequired(el) });
      }
    }
    return questions;
  }

  // --- deciding what a question is ------------------------------------------

  // Working in person: onsite, in office, hybrid, or a set number of days at an
  // office. Matches the wordings on the Greenhouse and Ashby forms checked,
  // like "in office 4 days/week" and "work from our US office three days per week".
  const IN_PERSON = /in[- ]person|in[- ]office|on[- ]?site|\bhybrid\b|\bfrom (our|the|an?)\b[^?]*\boffice\b|\bdays?\s*(a|per|\/)\s*week\b[^?]*\boffice\b|\boffice\b[^?]*\bdays?\s*(a|per|\/)\s*week\b/;

  // Order matters. Agreements are caught first, so an agreement that mentions
  // work authorization is never ticked. Sponsorship comes before authorization
  // because "authorized to work without sponsorship" is a sponsorship question,
  // and answering it as plain authorization would give the wrong answer.
  const RULES = [
    ["legal", (l) => /arbitrat|agreement|acknowledg|i (have )?read|consent|certif|attest|terms (of|and)|privacy (policy|notice)|ai policy/.test(l)],
    ["sponsorship", (l) => /sponsor/.test(l)],
    ["authorized", (l) => /authori[sz]ed to work|legally (eligible|able|permitted) to work|eligib\w* to work|right to work/.test(l)],
    ["relocation", (l) => /relocat/.test(l)],
    ["in_person", (l) => IN_PERSON.test(l)],
    ["gender", (l) => /\bgender\b/.test(l)],
    ["hispanic", (l) => /hispanic|latin[oxa]/.test(l)],
    ["race", (l) => /\brace\b|ethnicit/.test(l)],
    ["veteran", (l) => /veteran/.test(l)],
    ["disability", (l) => /disabilit/.test(l)],
    ["preferred_name", (l) => /preferred (first )?name/.test(l)],
    ["first_name", (l, el) => /first name|given name/.test(l) || el.name === "first_name"],
    ["last_name", (l, el) => /last name|surname|family name/.test(l) || el.name === "last_name"],
    ["full_name", (l, el) => /^(legal |full |your )?name\s*\*?$/.test(l) || /_systemfield_name$/.test(el.name || "")],
    ["email", (l, el) => /e-?mail/.test(l) || el.type === "email"],
    ["phone", (l, el) => /phone|mobile/.test(l) || el.type === "tel"],
    ["linkedin", (l) => /linkedin/.test(l)],
    ["github", (l) => /github/.test(l)],
    ["website", (l) => /website|portfolio|personal (site|url|page)/.test(l)],
    ["country", (l) => /^country\b/.test(l)],
    ["location", (l) => /^(where are you (currently )?(located|based)|current location|location|city)\b/.test(l)],
  ];

  // Keys a field's name or id may decide on its own, like Ashby's
  // "..._eeoc_gender". Work authorization, relocation, and in-person are
  // deliberately not among them: those are only ever answered from the
  // question's actual wording.
  const NAME_HINT_KEYS = new Set([
    "gender", "hispanic", "race", "veteran", "disability",
    "first_name", "last_name", "email", "phone", "linkedin", "github", "website", "country",
  ]);

  function classify(label, el) {
    const l = low(label);
    for (const [key, test] of RULES) if (test(l, el || {})) return key;
    const hint = low(String((el && (el.name || el.id)) || "").replace(/[_-]+/g, " "));
    if (hint) {
      for (const [key, test] of RULES) {
        if (NAME_HINT_KEYS.has(key) && test(hint, el || {})) return key;
      }
    }
    return null;
  }

  const EEO = { gender: "gender", hispanic: "hispanic_latino", race: "race", veteran: "veteran", disability: "disability" };

  function yesNo(value) {
    if (value === true || value === false) return value;
    const s = low(String(value ?? ""));
    if (["yes", "y", "true"].includes(s)) return true;
    if (["no", "n", "false"].includes(s)) return false;
    return null;
  }

  const asUrl = (v) => (!v ? null : /^https?:\/\//i.test(v) ? v : `https://${v}`);

  // Relocation and in-person questions are answered only when they ask whether
  // you are willing. Plenty of questions that mention relocation or an office
  // ask something else: "Do you need relocation assistance?", "Where would you
  // relocate to?", "Please confirm this role is onsite." Answering those from
  // a willingness would put a wrong answer on the application.
  const ASKS_WILLINGNESS = /\b(open|willing|able|prepared|comfortable|ok|okay|happy|commit\w*|available|interested)\b|\bcan you\b|\bwould you (consider|be)\b/;
  const RELOCATION_ASKS_SOMETHING_ELSE = /assist|package|stipend|reimburs|support|cover|cost|expense|benefit|bonus|\bwhere\b|which (city|cities|location|office)/;

  function workPreferenceAnswer(label, p) {
    const l = low(label);
    if (!ASKS_WILLINGNESS.test(l)) return null;

    const mentionsRelocation = /relocat/.test(l);
    if (mentionsRelocation && RELOCATION_ASKS_SOMETHING_ELSE.test(l)) return null;

    const prefs = p.work_preferences || {};
    const answers = [];
    if (mentionsRelocation) answers.push(yesNo(prefs.open_to_relocation));
    if (IN_PERSON.test(l)) answers.push(yesNo(prefs.willing_to_work_in_person));
    if (!answers.length || answers.includes(null)) return null;
    // A question asking about both is a yes only when both answers are yes.
    return { yesno: answers.every(Boolean) };
  }

  // A string, {yesno}, SKIP for a self-ID question left blank in the profile,
  // or null when the profile cannot answer the question truthfully.
  function answerFor(key, label, p) {
    const l = low(label);
    const wa = p.work_authorization || {};
    if (key in EEO) return norm((p.eeo || {})[EEO[key]] || "") || SKIP;
    if (key === "relocation" || key === "in_person") return workPreferenceAnswer(label, p);

    switch (key) {
      case "first_name": return p.first_name || null;
      case "last_name": return p.last_name || null;
      case "preferred_name": return p.preferred_name || SKIP;
      case "full_name": return norm(`${p.first_name || ""} ${p.last_name || ""}`) || null;
      case "email": return p.email || null;
      case "phone": return p.phone || null;
      case "linkedin": return asUrl(p.linkedin);
      case "github": return asUrl(p.github);
      case "website": return asUrl(p.website);
      case "country": return p.country || null;
      case "location": return p.location || null;

      case "sponsorship": {
        const needs = yesNo(wa.requires_sponsorship);
        if (needs === null) return null;
        if (/without (the )?(need for |requiring )?(any )?(visa |employer |employment )?sponsorship/.test(l)) {
          const authorized = yesNo(wa.authorized_to_work_us);
          return authorized === null ? null : { yesno: authorized && !needs };
        }
        if (/\b(require|need)s?\b[^?]*sponsor|sponsorship[^?]*\b(require|need)/.test(l)) return { yesno: needs };
        // Phrased some other way. A wrong answer here misrepresents your status,
        // so it is left for you.
        return null;
      }

      case "authorized": {
        const authorized = yesNo(wa.authorized_to_work_us);
        if (authorized === null) return null;
        const saysUS = /\bUS\b|\bU\.S\./.test(label) || /united states|\busa\b/i.test(label);
        const saysJobCountry = /country (where|in which) (the )?(job|role|position)/.test(l);
        return saysUS || (saysJobCountry && jobInUS()) ? { yesno: authorized } : null;
      }
    }
    return null;
  }

  // --- filling ----------------------------------------------------------------

  function setValue(el, value) {
    // React tracks input values through the prototype setter. Assigning
    // el.value directly changes the box but not React's state, so the field
    // would still read as empty when the form is submitted.
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const optionText = (o) => low(o.text ?? o.innerText ?? o.textContent ?? "");

  function bestOption(options, wanted) {
    if (wanted && typeof wanted === "object") {
      const re = wanted.yesno ? /^yes\b/ : /^no\b/;
      return options.find((o) => re.test(optionText(o))) || null;
    }
    const w = low(wanted);
    if (!w) return null;
    return (
      options.find((o) => optionText(o) === w) ||
      options.find((o) => optionText(o).startsWith(w)) ||
      (w.length >= 4 ? options.find((o) => optionText(o).includes(w)) : null) ||
      null
    );
  }

  function chooseSelect(select, wanted) {
    const pick = bestOption([...select.options], wanted);
    if (!pick) return false;
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set.call(select, pick.value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  // Opens a searchable dropdown, picks the matching option, and confirms the
  // choice took before calling it filled. Some dropdowns filter as you type;
  // others ignore typing and open only from the keyboard, or load their options
  // a moment after opening. It types first, falls back to the arrow key, and
  // waits for options to arrive. Tested against react-select 5, the widget
  // Greenhouse uses, in each of those configurations.
  async function chooseCombobox(input, wanted) {
    const typed = typeof wanted === "object" ? (wanted.yesno ? "Yes" : "No") : wanted;

    const currentOptions = () => {
      const listId = input.getAttribute("aria-controls") || input.getAttribute("aria-owns");
      const scope = (listId && document.getElementById(listId)) || document;
      return [...scope.querySelectorAll('[role="option"]')].filter(visible);
    };
    const waitForOptions = async () => {
      for (let attempt = 0; attempt < 8; attempt++) {
        await sleep(120);
        const found = currentOptions();
        if (found.length) return found;
      }
      return [];
    };
    const arrowDown = () => input.dispatchEvent(new KeyboardEvent("keydown", {
      key: "ArrowDown", code: "ArrowDown", keyCode: 40, which: 40, bubbles: true, cancelable: true,
    }));

    input.focus();
    input.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    if (!input.readOnly) setValue(input, typed);

    let options = await waitForOptions();
    if (!options.length) {
      arrowDown();
      options = await waitForOptions();
    }

    let pick = bestOption(options, wanted);
    // The typed filter matched nothing. On a list that ignores typing that
    // proves nothing, so clear it and look through the whole list.
    if (!pick && input.value) {
      setValue(input, "");
      arrowDown();
      pick = bestOption(await waitForOptions(), wanted);
    }

    if (!pick) {
      if (input.value) setValue(input, ""); // never leave a half-typed filter behind
      // Close the menu the arrow key opened. Blurring alone does not always
      // close it, and a list left hanging open covers the fields below.
      input.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true,
      }));
      input.blur();
      return false;
    }

    const chosen = norm(pick.innerText || pick.textContent);
    pick.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    pick.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    pick.click();
    await sleep(200);
    input.blur();
    await sleep(100);

    // Confirm the choice registered: the chosen text shows in the control, or
    // lands in the box itself. Stops below the dropdown's outer container,
    // since the open menu there holds every option's text too.
    if (norm(input.value) === chosen) return true;
    let box = input.parentElement;
    for (let depth = 0; box && depth < 3; depth++, box = box.parentElement) {
      if (norm(box.innerText).includes(chosen)) return true;
    }
    return false;
  }

  const clickControl = (input) => ((input.labels && input.labels[0]) || input).click();

  function chooseRadio(group, wanted) {
    const pick = bestOption(group.map((r) => ({ el: r, text: labelOf(r) })), wanted);
    if (!pick) return false;
    if (!pick.el.checked) clickControl(pick.el);
    return true;
  }

  function yesNoButtons(anchor) {
    let node = anchor.parentElement;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      const buttons = [...node.querySelectorAll("button")].filter((b) => /^(yes|no)$/i.test(norm(b.innerText)));
      if (buttons.length === 2) return buttons;
      if (buttons.length > 2) return null; // climbed into a neighbouring question
    }
    return null;
  }

  async function attachFile(input, resume) {
    const bytes = Uint8Array.from(atob(resume.base64), (c) => c.charCodeAt(0));
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], resume.filename, { type: resume.mime }));
    input.files = transfer.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(800);
    if (input.files && input.files.length) return true;
    // Some boards upload at once, clear the input, and show the filename instead.
    let node = input.parentElement;
    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
      if ((node.innerText || "").includes(resume.filename)) return true;
    }
    return false;
  }

  function highlight(q, status) {
    let target = q.el;
    if (q.kind === "combobox") target = q.el.closest('[class*="select__control"]') || q.el;
    if (["radio", "checkbox", "file"].includes(q.kind)) target = q.el.closest("fieldset") || q.el.parentElement || q.el;
    if (!target || !target.style) return;
    target.style.outline = `2px solid ${status === "needs" ? NEEDS : FILLED}`;
    target.style.outlineOffset = "2px";
  }

  // Why a recognized question was still left for you.
  function unansweredReason(key) {
    if (key === "sponsorship" || key === "authorized") {
      return "work authorization, phrased in a way it will not guess at";
    }
    if (key === "relocation" || key === "in_person") {
      return "asks something other than whether you are willing";
    }
    return "no answer in your profile";
  }

  async function handle(q, profile, resume) {
    const result = {
      label: norm(q.label).replace(/\s*\*$/, "").slice(0, 90) || "Unlabeled field",
      kind: q.kind,
      status: "needs",
      reason: "",
    };
    const done = (status, reason = "") => {
      result.status = status;
      result.reason = reason;
      if (status !== "skipped") highlight(q, status);
      return result;
    };

    if (q.kind === "textarea") return done("needs", "free response");

    if (q.kind === "file") {
      // A file input with neither a name nor an id is a helper, like Ashby's
      // "autofill from resume" dropzone. Dropping a resume there makes the
      // board re-parse it and overwrite the fields already filled.
      if (!q.el.name && !q.el.id) return done("skipped");
      const words = `${q.label} ${nearestHeading(q.el)} ${q.el.name} ${q.el.id}`;
      if (!/resume|\bcv\b/i.test(words)) {
        return q.required ? done("needs", "file upload") : done("skipped");
      }
      if (!resume) return done("needs", "no master resume uploaded yet");
      return (await attachFile(q.el, resume)) ? done("filled") : done("needs", "resume did not attach");
    }

    const key = classify(q.label, q.el);
    if (key === "legal") return done("needs", "agreement for you to accept");
    if (!key) return done("needs", "question it does not recognize");

    const answer = answerFor(key, q.label, profile);
    if (answer === SKIP) return q.required ? done("needs", "left blank in your profile") : done("skipped");
    if (answer === null) return done("needs", unansweredReason(key));

    switch (q.kind) {
      case "text":
        if (typeof answer !== "string") return done("needs", "expects a yes or no");
        if (norm(q.el.value)) return done("kept");
        // Typing into a read-only box changes nothing the form will submit, so
        // it would be wrong to report it as filled.
        if (q.el.readOnly || q.el.disabled) return done("needs", "read-only field");
        setValue(q.el, answer);
        return done("filled");
      case "select":
        if (q.el.selectedIndex > 0) return done("kept");
        return chooseSelect(q.el, answer) ? done("filled") : done("needs", "no matching option");
      case "combobox": {
        const box = q.el.closest('[class*="select__control"], [class*="container"]');
        if (box && box.querySelector('[class*="single-value"]')) return done("kept");
        return (await chooseCombobox(q.el, answer)) ? done("filled") : done("needs", "no matching option");
      }
      case "radio":
        if (q.group.some((r) => r.checked)) return done("kept");
        return chooseRadio(q.group, answer) ? done("filled") : done("needs", "no matching option");
      case "checkbox": {
        if (typeof answer !== "object") return done("needs", "question it does not recognize");
        const buttons = yesNoButtons(q.el);
        if (buttons) {
          buttons.find((b) => low(b.innerText) === (answer.yesno ? "yes" : "no")).click();
          return done("filled");
        }
        if (q.el.checked !== answer.yesno) clickControl(q.el);
        return done("filled");
      }
    }
    return done("needs", "question it does not recognize");
  }

  // --- entry points -------------------------------------------------------------

  async function run(profile, resume) {
    const submit = findSubmit();
    const scope = (submit && submit.closest("form")) || document.body;
    const questions = collect(scope);
    if (!questions.length) {
      return { error: "No application form on this page. On Ashby, open the Application tab first." };
    }

    const results = [];
    for (const q of questions) results.push(await handle(q, profile, resume));

    const needs = results.filter((r) => r.status === "needs");
    const reasons = [];
    if (captchaOnPage()) reasons.push("The form is protected by a CAPTCHA, which is yours to get past.");
    if (needs.length) reasons.push(`${needs.length} question${needs.length === 1 ? " needs" : "s need"} you.`);
    if (!submit) reasons.push("Could not find the submit button.");

    return {
      results,
      needs,
      filled: results.filter((r) => r.status === "filled").length,
      kept: results.filter((r) => r.status === "kept").length,
      submit,
      eligible: reasons.length === 0,
      reasons,
    };
  }

  // Checked again at the moment of submitting, since a CAPTCHA can appear
  // after the page loads.
  function submitNow(button) {
    if (window.__resumeAgentSubmitted) return { ok: false, reason: "it already submitted once on this page" };
    if (captchaOnPage()) return { ok: false, reason: "a CAPTCHA appeared" };
    if (!button || !button.isConnected) return { ok: false, reason: "the submit button is gone" };
    window.__resumeAgentSubmitted = true;
    button.click();
    return { ok: true };
  }

  window.__resumeAgentAutofill = { run, submitNow, captchaOnPage, _test: { classify, answerFor, bestOption, SKIP } };
})();
