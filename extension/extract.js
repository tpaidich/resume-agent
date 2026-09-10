// Pulls a job posting off the current page.
//
// Tried in order of reliability:
//   1. schema.org JobPosting in a JSON-LD block. Greenhouse, Lever, Ashby,
//      Workday, LinkedIn and Indeed all emit this, and it names the company
//      and title outright instead of leaving them to be guessed at.
//   2. Known per-board DOM selectors.
//   3. Generic page heuristics.
//
// Everything is namespaced on window.__resumeAgentExtract so content.js can
// call it without the two scripts fighting over globals.

(function () {
  const JOB_URL_HINTS = [
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "linkedin.com/jobs", "indeed.com", "smartrecruiters.com", "jobvite.com",
    "workable.com", "breezy.hr", "icims.com", "taleo.net", "bamboohr.com",
    "recruitee.com", "rippling.com", "pinpoint", "/careers", "/jobs/",
    "/job/", "job_app", "/apply",
  ];

  function stripHtml(html) {
    if (!html) return "";
    const el = document.createElement("div");
    el.innerHTML = html;
    el.querySelectorAll("script, style").forEach((n) => n.remove());
    return (el.innerText || el.textContent || "").trim();
  }

  function clean(text) {
    return (text || "").replace(/\n{3,}/g, "\n\n").replace(/[ \t]{2,}/g, " ").trim();
  }

  function metaContent(selector) {
    const el = document.querySelector(selector);
    return el ? (el.getAttribute("content") || "").trim() : "";
  }

  // --- Company name ---------------------------------------------------------
  // Worth doing carefully here: if the page does not name the company, the
  // server falls back to asking a model to read it out of the description,
  // which costs an extra call per posting. Every source below is free.

  const ATS_SLUG_HOSTS = [
    "greenhouse.io", "ashbyhq.com", "lever.co", "smartrecruiters.com",
    "workable.com", "breezy.hr", "recruitee.com",
  ];

  // Path segments that are part of the board's own URL structure, not a company.
  const NOT_A_SLUG = new Set([
    "jobs", "job", "careers", "career", "embed", "boards", "apply",
    "applications", "postings", "search", "en", "us", "company",
  ]);

  function titleCase(slug) {
    return slug
      .replace(/[-_]+/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase())
      .trim();
  }

  function companyFromUrl() {
    const host = location.hostname;
    if (!ATS_SLUG_HOSTS.some((h) => host.includes(h))) return "";
    for (const seg of location.pathname.split("/").filter(Boolean)) {
      if (NOT_A_SLUG.has(seg.toLowerCase())) continue;
      // Skip opaque ids: numeric, or a uuid.
      if (/^\d+$/.test(seg)) continue;
      if (/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(seg)) continue;
      return titleCase(seg);
    }
    return "";
  }

  function companyFromTitle() {
    // Boards title their pages "<Role> @ <Company>" or "... at <Company>".
    const t = document.title || "";
    const m = t.match(/\s[@|]\s*([^@|]+?)\s*$/) || t.match(/\s+at\s+([^-|@]+?)\s*$/i);
    if (!m) return "";
    const name = m[1].trim();
    return name.length >= 2 && name.length <= 60 ? name : "";
  }

  function resolveCompany(fromStructuredData) {
    return (
      (fromStructuredData || "").trim() ||
      companyFromTitle() ||
      metaContent('meta[property="og:site_name"]') ||
      metaContent('meta[name="application-name"]') ||
      companyFromUrl()
    );
  }

  // --- 1. JSON-LD -----------------------------------------------------------
  function fromJsonLd() {
    const blocks = document.querySelectorAll('script[type="application/ld+json"]');
    for (const block of blocks) {
      let parsed;
      try {
        parsed = JSON.parse(block.textContent);
      } catch (e) {
        continue; // a malformed block on the page is not our problem
      }
      // A block may be a single object, an array, or an @graph wrapper.
      const candidates = [];
      const push = (v) => { if (v && typeof v === "object") candidates.push(v); };
      if (Array.isArray(parsed)) parsed.forEach(push);
      else { push(parsed); if (Array.isArray(parsed["@graph"])) parsed["@graph"].forEach(push); }

      for (const node of candidates) {
        const type = node["@type"];
        const isJob = type === "JobPosting" ||
          (Array.isArray(type) && type.includes("JobPosting"));
        if (!isJob) continue;

        const org = node.hiringOrganization;
        const company = typeof org === "string" ? org : (org && org.name) || "";

        let location = "";
        const loc = node.jobLocation;
        const readLoc = (l) => {
          if (!l) return "";
          if (typeof l === "string") return l;
          const a = l.address || l;
          return [a.addressLocality, a.addressRegion, a.addressCountry]
            .filter((p) => typeof p === "string").join(", ");
        };
        location = Array.isArray(loc) ? readLoc(loc[0]) : readLoc(loc);

        const description = clean(stripHtml(node.description));
        if (description.length >= 200) {
          return {
            title: (node.title || "").trim(),
            company: resolveCompany(company),
            location: location.trim(),
            text: description,
            source: "json-ld",
          };
        }
      }
    }
    return null;
  }

  // --- 2. Per-board selectors ----------------------------------------------
  const BOARD_SELECTORS = [
    { host: "greenhouse.io", title: "h1", body: "#content, .job__description, .body" },
    { host: "lever.co", title: ".posting-headline h2, h2", body: ".section-wrapper, .posting-page" },
    { host: "ashbyhq.com", title: "h1", body: '[class*="_descriptionText"], main' },
    { host: "myworkdayjobs.com", title: 'h1, [data-automation-id="jobPostingHeader"]', body: '[data-automation-id="jobPostingDescription"]' },
    { host: "linkedin.com", title: ".top-card-layout__title, h1", body: ".description__text, .show-more-less-html__markup" },
    { host: "indeed.com", title: 'h1, [data-testid="jobsearch-JobInfoHeader-title"]', body: "#jobDescriptionText" },
    { host: "smartrecruiters.com", title: "h1", body: ".job-sections, #st-jobDescription" },
    { host: "workable.com", title: "h1", body: '[data-ui="job-description"], main' },
  ];

  function fromBoard() {
    const host = location.hostname;
    for (const board of BOARD_SELECTORS) {
      if (!host.includes(board.host)) continue;
      const bodyEl = document.querySelector(board.body);
      if (!bodyEl) continue;
      const text = clean(bodyEl.innerText);
      if (text.length < 200) continue;
      const titleEl = document.querySelector(board.title);
      return {
        title: titleEl ? clean(titleEl.innerText) : "",
        company: resolveCompany(""),
        location: "",
        text,
        source: `board:${board.host}`,
      };
    }
    return null;
  }

  // --- 3. Generic ----------------------------------------------------------
  function fromGeneric() {
    const main = document.querySelector("main, article, [role='main']") || document.body;
    const clone = main.cloneNode(true);
    clone.querySelectorAll("script, style, nav, header, footer, aside, form").forEach((n) => n.remove());
    const text = clean(clone.innerText);
    if (text.length < 200) return null;

    const h1 = document.querySelector("h1");
    return {
      title: h1 ? clean(h1.innerText) : clean(document.title),
      company: resolveCompany(""),
      location: "",
      text,
      source: "generic",
    };
  }

  // --- Is this one job posting? --------------------------------------------
  // Deliberately strict. The earlier version matched any URL containing
  // "greenhouse.io" or "/careers", which is true of a board's index page, so
  // it scored listings as though they were postings.

  // URL shapes that identify a single posting, not a board or a search.
  const POSTING_URL_PATTERNS = [
    /greenhouse\.io\/[^/]+\/jobs\/\d+/i,
    /jobs\.ashbyhq\.com\/[^/]+\/[0-9a-f]{8}-[0-9a-f]{4}-/i,
    /jobs\.lever\.co\/[^/]+\/[0-9a-f-]{20,}/i,
    /myworkdayjobs\.com\/.+\/job\/[^/]+\/[^/]+/i,
    /linkedin\.com\/jobs\/view\/\d+/i,
    /indeed\.com\/(viewjob\?|job\/)/i,
    /smartrecruiters\.com\/[^/]+\/\d{6,}/i,
    /workable\.com\/j\/[A-Z0-9]{6,}/i,
    /jobvite\.com\/.*\/job\/[A-Za-z0-9]+/i,
    /icims\.com\/jobs\/\d+/i,
    /breezy\.hr\/p\/[0-9a-f]+/i,
    /recruitee\.com\/o\/[a-z0-9-]+/i,
    // Generic: a /job/ or /jobs/ path ending in an id or slug, not a bare index.
    /\/jobs?\/[A-Za-z0-9][A-Za-z0-9._-]{3,}\/?$/,
  ];

  function isPostingUrl() {
    return POSTING_URL_PATTERNS.some((re) => re.test(location.href));
  }

  function hasJobPostingJsonLd() {
    for (const block of document.querySelectorAll('script[type="application/ld+json"]')) {
      let parsed;
      try { parsed = JSON.parse(block.textContent); } catch (e) { continue; }
      const nodes = [];
      const push = (v) => { if (v && typeof v === "object") nodes.push(v); };
      if (Array.isArray(parsed)) parsed.forEach(push);
      else { push(parsed); if (Array.isArray(parsed["@graph"])) parsed["@graph"].forEach(push); }
      for (const n of nodes) {
        const t = n["@type"];
        if (t === "JobPosting" || (Array.isArray(t) && t.includes("JobPosting"))) return true;
      }
    }
    return false;
  }

  // A board index links out to many postings; a single posting links to very
  // few. This is what separates a careers page from a job on that careers page.
  function countPostingLinks() {
    const seen = new Set();
    for (const a of document.querySelectorAll("a[href]")) {
      const href = a.href;
      if (!href || href === location.href) continue;
      if (POSTING_URL_PATTERNS.some((re) => re.test(href))) seen.add(href.split("#")[0]);
      if (seen.size > 6) break;
    }
    return seen.size;
  }

  function hasApplyAffordance() {
    const text = (document.body ? document.body.innerText : "").toLowerCase();
    return /\bapply\s+(now|for this|to this)\b|\bsubmit\s+application\b|\bapplication\s+form\b/.test(text);
  }

  function sectionHits() {
    const body = document.body ? document.body.innerText.toLowerCase() : "";
    const words = ["responsibilities", "qualifications", "what you'll do",
                   "what you will do", "about the role", "requirements",
                   "minimum qualifications", "preferred qualifications",
                   "who you are", "what we're looking for"];
    return words.filter((w) => body.includes(w)).length;
  }

  function looksLikeJobPage() {
    // Structured data for a posting is definitive.
    if (hasJobPostingJsonLd()) return true;

    // Several outbound posting links means this is an index, whatever its URL.
    if (countPostingLinks() >= 3) return false;

    if (isPostingUrl()) return true;

    // Unrecognized site: require an apply affordance and real requirement
    // sections before assuming this is a posting.
    return hasApplyAffordance() && sectionHits() >= 2;
  }

  function extract() {
    const found = fromJsonLd() || fromBoard() || fromGeneric();
    if (!found) return null;
    if (!found.title) found.title = clean(document.title);
    // The description is capped because the scorer only reads the first few
    // thousand characters anyway, and huge payloads slow the round trip.
    found.text = found.text.slice(0, 20000);
    found.url = location.href.split("#")[0];
    return found;
  }

  window.__resumeAgentExtract = { extract, looksLikeJobPage };
})();
