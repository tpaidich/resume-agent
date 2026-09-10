import sys
import os
import yaml

from tools.scraper import scrape_job
from tools.scorer import score_fit
from tools.tailor import tailor_resume
from tools.latex_compiler import compile_pdf
from tools.tracker import track_application

MASTER_RESUME = "resume/master.yaml"
MIN_SCORE = 50


def main(url: str):
    print(f"\n[agent] URL: {url}")

    # 1. Scrape
    print("[agent] Scraping job posting...")
    job = scrape_job(url)
    print(f"[agent] Found: {job['title']} at {job['company']}")

    # 2. Load master resume
    with open(MASTER_RESUME) as f:
        master = yaml.safe_load(f)

    # 3. Score
    print("[agent] Scoring fit...")
    score = score_fit(job, master)
    print(f"[agent] Score: {score['score']}/100")
    if score.get("strengths"):
        print(f"[agent] Strengths: {', '.join(score['strengths'][:3])}")
    if score.get("gaps"):
        print(f"[agent] Gaps: {', '.join(score['gaps'][:3])}")

    if score.get("excludes_sponsorship"):
        print("[agent] Job explicitly excludes sponsorship — skipping.")
        track_application(url, job, score, status="ineligible")
        return

    if score["score"] < MIN_SCORE:
        print(f"[agent] Score below threshold ({MIN_SCORE}), skipping PDF generation.")
        track_application(url, job, score, status="skipped")
        return

    # 4. Tailor
    print("[agent] Tailoring resume...")
    placeholders = tailor_resume(job, master, gaps=score.get("gaps", []))

    # 5. Compile PDF
    print("[agent] Compiling PDF...")
    try:
        pdf_path = compile_pdf(placeholders, job)
        print(f"[agent] PDF saved: {pdf_path}")
        track_application(url, job, score, pdf_path=pdf_path, status="ready")
    except RuntimeError as e:
        print(f"[agent] PDF compilation failed: {e}")
        print("[agent] Saving filled LaTeX instead...")
        import json
        fallback = f"applications/{job.get('company', 'unknown')}_{job.get('title', 'role')}_tailored.json"
        os.makedirs("applications", exist_ok=True)
        with open(fallback, "w") as f:
            json.dump(placeholders, f, indent=2)
        track_application(url, job, score, pdf_path=fallback, status="pdf_failed")

    print("[agent] Done.\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent.py <job_url>")
        sys.exit(1)
    main(sys.argv[1])
