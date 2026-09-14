"""Streamlit review queue for sourced job postings.

The one thing this app does not do is apply to anything. There is no button
here that submits a form or calls an ATS apply endpoint. "Interested" generates
a tailored resume PDF and records its path; taking that PDF and applying is a
human step, on purpose.

Run from the repo root:
    streamlit run dashboard/review_app.py
"""
import os
import sys

# Import the repo's own modules, and run with the repo root as CWD, because
# tools/tailor.py and tools/latex_compiler.py resolve resume/ and applications/
# relative to the process working directory.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import yaml
import streamlit as st

from tools.latex_compiler import compile_pdf
from tools.sourcing import job_store
from tools.sourcing.paths import MASTER_RESUME
from tools.tailor import tailor_resume

# Streamlit's own colour names are used rather than hex, so the badges stay
# legible in both the light and dark themes.
SPONSORSHIP_LABELS = {
    "yes": ("Sponsors", "green"),
    "no": ("No sponsorship", "red"),
    "flagged_no_sponsorship_in_jd": ("JD rules out sponsorship", "red"),
    "unknown": ("Sponsorship unknown", "gray"),
}

st.set_page_config(page_title="Job Review Queue", page_icon="🗂️", layout="wide")

job_store.init_db()


@st.cache_data(show_spinner=False)
def _load_master():
    with open(MASTER_RESUME) as f:
        return yaml.safe_load(f)


def _sponsorship_badge(status: str) -> str:
    label, color = SPONSORSHIP_LABELS.get(status, SPONSORSHIP_LABELS["unknown"])
    return f":{color}[**{label}**]"


def _generate_resume(row: dict) -> str:
    """Run the existing tailor + compile pipeline for one posting.

    Returns the PDF path. Raises on failure so the caller can surface it.
    """
    job = {
        "title": row["title"],
        "company": row["company"],
        "url": row["url"],
        "description": row["description_text"] or "",
    }
    placeholders = tailor_resume(job, _load_master(), gaps=[])
    return compile_pdf(placeholders, job)


# --- Header -----------------------------------------------------------------
st.title("Job Review Queue")
st.caption(
    "Sourced from public company job boards. Nothing here applies on your "
    "behalf; Interested only generates a tailored resume for you to send."
)

counts = job_store.counts_by_status()
cols = st.columns(4)
for col, key, label in zip(
    cols,
    ["new", "interested", "resume_generated", "rejected"],
    ["New", "Interested", "Resume generated", "Rejected"],
):
    col.metric(label, counts.get(key, 0))

# --- Filters ----------------------------------------------------------------
with st.sidebar:
    st.header("Filters")

    view = st.radio(
        "Queue",
        ["New", "Resume generated", "Rejected", "All"],
        index=0,
    )
    view_map = {
        "New": ["new"],
        "Resume generated": ["resume_generated", "interested"],
        "Rejected": ["rejected"],
        "All": None,
    }

    min_score = st.slider("Minimum match score", 1, 10, 1)
    include_unscored = st.checkbox("Include unscored postings", value=True)
    max_age_days = st.number_input(
        "Posted within (days; 0 for any age)", min_value=0, max_value=365, value=7, step=1
    )

    sponsorship_choice = st.multiselect(
        "Sponsorship status",
        options=list(SPONSORSHIP_LABELS.keys()),
        default=list(SPONSORSHIP_LABELS.keys()),
        format_func=lambda s: SPONSORSHIP_LABELS[s][0],
    )

    company_options = job_store.all_companies()
    company_choice = st.multiselect("Company", options=company_options, default=[])

    st.divider()
    st.caption(
        "Unknown sponsorship stays in the queue on purpose. An absent "
        "disclaimer is not evidence either way, so it is shown rather than "
        "guessed at."
    )

rows = job_store.query_jobs(
    review_status=view_map[view],
    min_score=min_score if min_score > 1 else None,
    sponsorship_status=sponsorship_choice or None,
    companies=company_choice or None,
    include_unscored=include_unscored,
    max_age_days=max_age_days or None,
)

st.write(f"**{len(rows)}** postings")

if not rows:
    st.info("Nothing matches these filters. Run a polling pass to source new postings.")

# --- Queue ------------------------------------------------------------------
for row in rows:
    job_id = row["job_id"]
    score = row["match_score"]
    score_text = f"{score}/10" if score is not None else "unscored"

    with st.container(border=True):
        head, actions = st.columns([5, 1.4])

        with head:
            st.markdown(f"### {row['title']}")
            meta = " · ".join(
                p for p in [row["company"], row["location"] or None, row["ats_source"]] if p
            )
            st.markdown(
                f"{meta} &nbsp;·&nbsp; **{score_text}** &nbsp;·&nbsp; "
                f"{_sponsorship_badge(row['sponsorship_status'])}"
            )
            if row["match_reason"]:
                st.markdown(f"_{row['match_reason']}_")
            if row["url"]:
                st.markdown(f"[Open posting]({row['url']})")
            if row["resume_pdf_path"]:
                st.success(f"Resume: {row['resume_pdf_path']}")

        with actions:
            if row["review_status"] == "new":
                if st.button("Interested", key=f"yes_{job_id}", type="primary",
                             use_container_width=True):
                    with st.spinner("Tailoring resume and compiling PDF..."):
                        try:
                            pdf_path = _generate_resume(row)
                            job_store.set_review_status(job_id, "resume_generated", pdf_path)
                            st.success("Resume generated.")
                        except Exception as e:
                            # Mark interest even when the PDF step fails, so the
                            # decision is not lost and can be retried.
                            job_store.set_review_status(job_id, "interested")
                            st.error(f"Resume generation failed: {e}")
                    st.rerun()

                if st.button("Reject", key=f"no_{job_id}", use_container_width=True):
                    job_store.set_review_status(job_id, "rejected")
                    st.rerun()

            elif row["review_status"] == "interested":
                if st.button("Retry resume", key=f"retry_{job_id}", type="primary",
                             use_container_width=True):
                    with st.spinner("Tailoring resume and compiling PDF..."):
                        try:
                            pdf_path = _generate_resume(row)
                            job_store.set_review_status(job_id, "resume_generated", pdf_path)
                            st.success("Resume generated.")
                        except Exception as e:
                            st.error(f"Resume generation failed: {e}")
                    st.rerun()
                if st.button("Reject", key=f"no2_{job_id}", use_container_width=True):
                    job_store.set_review_status(job_id, "rejected")
                    st.rerun()

            else:
                if st.button("Back to queue", key=f"back_{job_id}", use_container_width=True):
                    job_store.set_review_status(job_id, "new")
                    st.rerun()

        if row["description_text"]:
            with st.expander("Job description"):
                st.text(row["description_text"][:8000])
