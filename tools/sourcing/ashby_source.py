"""Fetch postings from a public Ashby job board.

Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
No authentication, no pagination — one request returns the whole board. A 404
means the company is not on Ashby (or moved away); logged and skipped.

Ashby already hands back plain text in descriptionPlain, so no HTML stripping
is needed here.
"""
import requests

from .job_store import make_job_id

API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ResumeAgent/1.0)"}
ATS = "ashby"


def fetch_jobs(token: str, company: str, timeout: int = 20) -> list[dict]:
    """Return normalized posting dicts. Never raises on a bad board."""
    try:
        resp = requests.get(API.format(token=token), headers=_HEADERS, timeout=timeout)
    except requests.RequestException as e:
        print(f"[ashby] {company} ({token}): request failed — {e}")
        return []

    if resp.status_code == 404:
        print(f"[ashby] {company} ({token}): 404 — not on Ashby or wrong token. Skipping.")
        return []
    if resp.status_code != 200:
        print(f"[ashby] {company} ({token}): HTTP {resp.status_code}. Skipping.")
        return []

    try:
        payload = resp.json()
    except ValueError:
        print(f"[ashby] {company} ({token}): response was not JSON. Skipping.")
        return []

    jobs = []
    for raw in payload.get("jobs", []):
        # Ashby includes unlisted postings; those are not open to applicants.
        if raw.get("isListed") is False:
            continue

        title = (raw.get("title") or "").strip()
        if not title:
            continue

        url = raw.get("jobUrl") or raw.get("applyUrl") or ""

        location = raw.get("location") or ""
        if raw.get("isRemote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()

        department = " / ".join(
            p for p in (raw.get("department"), raw.get("team")) if p
        )

        jobs.append({
            "job_id": make_job_id(ATS, raw.get("id"), company, title, url),
            "company": company,
            "title": title,
            "location": location,
            "ats_source": ATS,
            "url": url,
            "description_text": raw.get("descriptionPlain") or "",
            "published_at": raw.get("publishedAt") or "",
            "department": department,
        })

    print(f"[ashby] {company} ({token}): {len(jobs)} postings")
    return jobs
