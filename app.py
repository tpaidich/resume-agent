import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st
import yaml

from tools.scraper import scrape_job
from tools.scorer import score_fit, score_jobs_batch
from tools.tailor import tailor_resume
from tools.latex_compiler import compile_pdf
from tools.tracker import track_application, TRACKER_FILE
from tools.finder import find_jobs, filter_jobs, ROLE_KEYWORDS

MIN_SCORE = 50
MUST_APPLY = 70

st.set_page_config(page_title="Resume Agent", page_icon="📄", layout="centered")
st.title("Resume Agent")

# --- Find Jobs ---------------------------------------------------------------
with st.expander("Find Jobs", expanded=False):
    all_categories = list(ROLE_KEYWORDS.keys())
    default = [
        "Data Science", "Data Analytics", "Business Analytics",
        "Project Analyst", "Project Manager", "Consulting",
        "Tech Consulting", "Data Engineering",
    ]
    selected = st.multiselect("Role categories", all_categories, default=default)

    if st.button("Search & Score Jobs", type="primary", disabled=not selected):
        with open("resume/master.yaml") as f:
            _master = yaml.safe_load(f)

        # Phase 1: find + title filter
        with st.spinner("Searching job boards..."):
            raw = find_jobs(selected)
            filtered = filter_jobs(raw)
        st.write(f"Found **{len(raw)}** jobs, **{len(filtered)}** after filtering seniors/irrelevant.")

        if not filtered:
            st.warning("No matching jobs found. Try different categories.")
        else:
            # Cap at 20 to keep runtime reasonable
            capped = filtered[:20]

            # Phase 2a: scrape all jobs in parallel
            st.write(f"Scraping **{len(capped)}** jobs in parallel...")
            progress = st.progress(0)
            scraped_jobs: list[dict] = []
            scrape_done = [0]

            def _scrape(stub):
                return scrape_job(stub["url"])

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(_scrape, stub): stub for stub in capped}
                for future in as_completed(futures, timeout=180):
                    scrape_done[0] += 1
                    progress.progress(scrape_done[0] / (len(capped) + 1))
                    try:
                        scraped_jobs.append(future.result(timeout=30))
                    except Exception:
                        pass

            # Phase 2b: score all in ONE batch call
            progress.progress(len(capped) / (len(capped) + 1))
            st.write(f"Scoring **{len(scraped_jobs)}** jobs (1 API call)...")
            scores = score_jobs_batch(scraped_jobs, _master)
            progress.progress(1.0)

            must_apply, for_review = [], []
            for job, score in zip(scraped_jobs, scores):
                if score.get("excludes_sponsorship") or score["score"] < MIN_SCORE:
                    continue
                entry = {
                    "title":     job["title"],
                    "company":   job["company"],
                    "url":       job["url"],
                    "score":     score["score"],
                    "strengths": score.get("strengths", []),
                    "gaps":      score.get("gaps", []),
                }
                if score["score"] >= MUST_APPLY:
                    must_apply.append(entry)
                else:
                    for_review.append(entry)

            progress.empty()

            def _show_scored(entries, label, key):
                st.subheader(f"{label} ({len(entries)})")
                if not entries:
                    st.caption("None.")
                    return
                df = pd.DataFrame(entries).sort_values("score", ascending=False)
                df["Queue?"] = False
                edited = st.data_editor(
                    df[["Queue?", "score", "company", "title", "url"]],
                    use_container_width=True, hide_index=True,
                    column_config={
                        "Queue?": st.column_config.CheckboxColumn("Queue?", default=False),
                        "url":    st.column_config.LinkColumn("Link", display_text="View"),
                        "score":  st.column_config.NumberColumn("Score", format="%d%%"),
                    },
                    disabled=["score", "company", "title", "url"],
                    key=key,
                )
                return edited

            e_must   = _show_scored(must_apply, "Must Apply — 70%+", "found_must")
            e_review = _show_scored(for_review, "For Review — 50–70%", "found_review")

            all_queued = []
            for edited_df, entries in [(e_must, must_apply), (e_review, for_review)]:
                if edited_df is not None:
                    q = edited_df[edited_df["Queue?"] == True]
                    all_queued.extend(q["url"].tolist())

            if all_queued:
                if st.button(f"Generate resumes for {len(all_queued)} selected"):
                    st.session_state["queued_urls"] = all_queued
                    st.rerun()

# --- Helpers -----------------------------------------------------------------
def load_tracker():
    if not os.path.exists(TRACKER_FILE):
        return []
    with open(TRACKER_FILE) as f:
        return json.load(f)

def save_tracker(data):
    with open(TRACKER_FILE, "w") as f:
        json.dump(data, f, indent=2)

def run_pipeline(url: str, master: dict, force: bool = False):
    """Scrape → score → tailor → compile. force=True skips the score gate."""

    with st.status("Scraping job posting...", expanded=True) as s:
        try:
            job = scrape_job(url)
            st.write(f"**{job['title']}** at **{job['company']}**")
            s.update(label=f"Scraped: {job['title']} at {job['company']}", state="complete")
        except Exception as e:
            s.update(label=f"Scrape failed: {e}", state="error")
            return

    with st.status("Scoring fit with Claude...", expanded=True) as s:
        try:
            score = score_fit(job, master)
            col1, col2 = st.columns([1, 3])
            col1.metric("Fit Score", f"{score['score']}/100")
            with col2:
                strengths = score.get("strengths", [])
                gaps = score.get("gaps", [])
                if strengths:
                    st.markdown(f"**Strengths ({len(strengths)})**")
                    for item in strengths:
                        st.markdown(f"- {item}")
                if gaps:
                    st.markdown(f"**Gaps ({len(gaps)})**")
                    for item in gaps:
                        st.markdown(f"- {item}")
            s.update(label=f"Fit score: {score['score']}/100", state="complete")
        except Exception as e:
            s.update(label=f"Scoring failed: {e}", state="error")
            return

    if not force:
        if score.get("excludes_sponsorship"):
            st.error("This job explicitly excludes visa sponsorship — skipping.")
            track_application(url, job, score, status="ineligible")
            return
        if score["score"] < MIN_SCORE:
            st.warning(f"Score {score['score']}/100 is below threshold ({MIN_SCORE}) — skipping.")
            track_application(url, job, score, status="skipped")
            return
    else:
        st.info("Override active — skipping score and sponsorship gates.")

    with st.status("Tailoring resume with Claude...", expanded=True) as s:
        try:
            placeholders = tailor_resume(job, master, gaps=score.get("gaps", []))
            s.update(label="Resume tailored", state="complete")
        except Exception as e:
            s.update(label=f"Tailoring failed: {e}", state="error")
            return

    with st.status("Compiling PDF...", expanded=True) as s:
        try:
            pdf_path = compile_pdf(placeholders, job)
            s.update(label=f"PDF ready: {pdf_path}", state="complete")
            with open(pdf_path, "rb") as f:
                st.download_button(
                    "Download PDF",
                    f,
                    file_name=os.path.basename(pdf_path),
                    mime="application/pdf",
                )
            track_application(url, job, score, pdf_path=pdf_path, status="ready")
        except RuntimeError:
            s.update(label="pdflatex not installed — saving JSON fallback", state="error")
            fallback = f"applications/{job.get('company','co')}_{job.get('title','role')}_tailored.json"
            os.makedirs("applications", exist_ok=True)
            with open(fallback, "w") as f:
                json.dump(placeholders, f, indent=2)
            track_application(url, job, score, pdf_path=fallback, status="pdf_failed")
            st.info(f"Tailored content saved to `{fallback}`")

    st.success("Done!")

# --- Main run ----------------------------------------------------------------
url = st.text_input("Job posting URL", placeholder="https://boards.greenhouse.io/stripe/jobs/...")

if st.button("Run", type="primary", disabled=not url):
    with open("resume/master.yaml") as f:
        master = yaml.safe_load(f)
    run_pipeline(url, master, force=False)

# --- Batch run from Find Jobs queue ------------------------------------------
if st.session_state.get("queued_urls"):
    queued_urls = st.session_state.pop("queued_urls")
    with open("resume/master.yaml") as f:
        master = yaml.safe_load(f)
    st.divider()
    st.subheader(f"Running pipeline on {len(queued_urls)} queued job(s)")
    for i, q_url in enumerate(queued_urls):
        st.markdown(f"**Job {i+1} of {len(queued_urls)}:** {q_url}")
        run_pipeline(q_url, master, force=False)
        st.divider()

# --- Override from session state (triggered by skipped table) ----------------
if st.session_state.get("override_url"):
    override_url = st.session_state.pop("override_url")
    st.divider()
    st.subheader(f"Override: generating resume for {override_url}")
    with open("resume/master.yaml") as f:
        master = yaml.safe_load(f)
    run_pipeline(override_url, master, force=True)

# --- Tracker -----------------------------------------------------------------
st.divider()

tracker = load_tracker()

active     = [e for e in tracker if e.get("status") not in ("applied", "skipped", "ineligible")]
skipped    = [e for e in tracker if e.get("status") == "skipped"]
ineligible = [e for e in tracker if e.get("status") == "ineligible"]
applied    = [e for e in tracker if e.get("status") == "applied"]

must_apply   = sorted([e for e in active if (e.get("score") or 0) >= MUST_APPLY], key=lambda x: -x.get("score", 0))
for_review   = sorted([e for e in active if (e.get("score") or 0) < MUST_APPLY],  key=lambda x: -x.get("score", 0))

def render_active_table(entries: list, key: str):
    if not entries:
        st.caption("None.")
        return

    df = pd.DataFrame(entries)
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.strftime("%b %d %H:%M")
    df["Applied?"] = False
    df["Remove?"] = False
    cols = ["Applied?", "Remove?", "timestamp", "company", "title", "score", "status", "url", "pdf"]
    cols = [c for c in cols if c in df.columns or c in ("Applied?", "Remove?")]

    edited = st.data_editor(
        df[cols], use_container_width=True, hide_index=True,
        column_config={
            "Applied?": st.column_config.CheckboxColumn("Applied?", default=False),
            "Remove?":  st.column_config.CheckboxColumn("Remove?",  default=False),
            "url":      st.column_config.LinkColumn("Link", display_text="View"),
            "pdf":      st.column_config.TextColumn("PDF", width="medium"),
        },
        disabled=[c for c in cols if c not in ("Applied?", "Remove?")],
        key=key,
    )

    newly_applied = edited[edited["Applied?"] == True].index.tolist()
    to_remove     = edited[edited["Remove?"]  == True].index.tolist()
    col_a, col_b  = st.columns(2)

    if newly_applied and col_a.button(f"Confirm {len(newly_applied)} submitted", key=f"{key}_apply"):
        for i in newly_applied:
            u = entries[i]["url"]
            for entry in tracker:
                if entry["url"] == u:
                    entry["status"] = "applied"
        save_tracker(tracker)
        st.rerun()

    if to_remove and col_b.button(f"Remove {len(to_remove)}", key=f"{key}_remove"):
        bad = {entries[i]["url"] for i in to_remove}
        tracker[:] = [e for e in tracker if e["url"] not in bad]
        save_tracker(tracker)
        st.rerun()

# Must Apply
st.subheader(f"Must Apply — 70%+ ({len(must_apply)})")
render_active_table(must_apply, "must_apply_editor")

# For Review
st.subheader(f"For Review — 50–70% ({len(for_review)})")
render_active_table(for_review, "for_review_editor")

# Skipped (low score — override allowed)
st.subheader(f"Skipped — Low Score ({len(skipped)})")
if skipped:
    for i, entry in enumerate(skipped):
        col1, col2, col3 = st.columns([3, 1, 1])
        col1.write(f"**{entry.get('company')}** — {entry.get('title')}  \n"
                   f"Score: {entry.get('score')}")
        if col2.button("Generate Resume", key=f"override_{i}"):
            st.session_state["override_url"] = entry["url"]
            st.rerun()
        if col3.button("Remove", key=f"remove_skipped_{i}"):
            tracker[:] = [e for e in tracker if e["url"] != entry["url"]]
            save_tracker(tracker)
            st.rerun()
else:
    st.caption("None.")

# Ineligible (no sponsorship — hard no, no override)
st.subheader(f"Ineligible — No Sponsorship ({len(ineligible)})")
if ineligible:
    for i, entry in enumerate(ineligible):
        col1, col2 = st.columns([4, 1])
        col1.write(f"**{entry.get('company')}** — {entry.get('title')}")
        if col2.button("Remove", key=f"remove_ineligible_{i}"):
            tracker[:] = [e for e in tracker if e["url"] != entry["url"]]
            save_tracker(tracker)
            st.rerun()
else:
    st.caption("None.")

# Applied
st.subheader(f"Applied ({len(applied)})")
if applied:
    df_applied = pd.DataFrame(applied)
    df_applied["timestamp"] = pd.to_datetime(df_applied["timestamp"]).dt.strftime("%b %d %H:%M")
    show = [c for c in ["timestamp", "company", "title", "score", "url", "pdf"] if c in df_applied.columns]
    st.dataframe(df_applied[show], use_container_width=True, hide_index=True,
                 column_config={"url": st.column_config.LinkColumn("Link", display_text="View")})
else:
    st.caption("No applications submitted yet.")
