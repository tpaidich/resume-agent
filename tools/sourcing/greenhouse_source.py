"""Fetch postings from a public Greenhouse job board.

Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
No authentication. A 404 means the token is wrong or the company has migrated
off Greenhouse; that is logged and skipped rather than raised, so one dead
token cannot take down a whole polling run.
"""
import html
import requests
from bs4 import BeautifulSoup

from .job_store import make_job_id

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ResumeAgent/1.0)"}
ATS = "greenhouse"


def _plain_text(content_html: str) -> str:
    """Greenhouse returns the JD as HTML-escaped HTML. Unescape, then strip tags."""
    if not content_html:
        return ""
    unescaped = html.unescape(content_html)
    return BeautifulSoup(unescaped, "html.parser").get_text(separator="\n", strip=True)


def fetch_jobs(token: str, company: str, timeout: int = 20) -> list[dict]:
    """Return normalized posting dicts. Never raises on a bad board."""
    try:
        resp = requests.get(API.format(token=token), headers=_HEADERS, timeout=timeout)
    except requests.RequestException as e:
        print(f"[greenhouse] {company} ({token}): request failed — {e}")
        return []

    if resp.status_code == 404:
        print(f"[greenhouse] {company} ({token}): 404 — wrong token or migrated off Greenhouse. Skipping.")
        return []
    if resp.status_code != 200:
        print(f"[greenhouse] {company} ({token}): HTTP {resp.status_code}. Skipping.")
        return []

    try:
        payload = resp.json()
    except ValueError:
        print(f"[greenhouse] {company} ({token}): response was not JSON. Skipping.")
        return []

    jobs = []
    for raw in payload.get("jobs", []):
        title = (raw.get("title") or "").strip()
        url = raw.get("absolute_url") or ""
        if not title:
            continue

        location = (raw.get("location") or {}).get("name") or ""
        departments = ", ".join(
            d.get("name", "") for d in (raw.get("departments") or []) if d.get("name")
        )

        jobs.append({
            "job_id": make_job_id(ATS, raw.get("id"), company, title, url),
            "company": company,
            "title": title,
            "location": location,
            "ats_source": ATS,
            "url": url,
            "description_text": _plain_text(raw.get("content") or ""),
            "published_at": raw.get("first_published") or raw.get("updated_at") or "",
            "department": departments,
        })

    print(f"[greenhouse] {company} ({token}): {len(jobs)} postings")
    return jobs
