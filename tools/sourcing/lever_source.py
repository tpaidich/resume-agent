"""Fetch postings from a public Lever job board.

Endpoint: GET https://api.lever.co/v0/postings/{token}?mode=json
No authentication; one request returns the whole board as a JSON list. A 404
({"ok": false}) means the company is not on Lever; logged and skipped.

Lever splits a description into an intro, a series of titled bullet lists, and
a closing section. They are joined back together so the keyword gate and the
sponsorship scan see the whole text, the same as the other sources.
"""
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from .job_store import make_job_id

API = "https://api.lever.co/v0/postings/{token}?mode=json"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ResumeAgent/1.0)"}
ATS = "lever"


def _description(raw: dict) -> str:
    parts = [raw.get("descriptionPlain") or ""]
    for section in raw.get("lists") or []:
        body = BeautifulSoup(section.get("content") or "", "html.parser").get_text(
            separator="\n", strip=True
        )
        parts.append(f"{section.get('text', '')}\n{body}".strip())
    parts.append(raw.get("additionalPlain") or "")
    return "\n\n".join(p for p in parts if p)


def _published(created_ms) -> str:
    if not isinstance(created_ms, (int, float)):
        return ""
    return datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def fetch_jobs(token: str, company: str, timeout: int = 20) -> list[dict]:
    """Return normalized posting dicts. Never raises on a bad board."""
    # Lever resets the occasional connection outright; one retry clears it.
    resp = None
    for attempt in (1, 2):
        try:
            resp = requests.get(API.format(token=token), headers=_HEADERS, timeout=timeout)
            break
        except requests.ConnectionError as e:
            if attempt == 2:
                print(f"[lever] {company} ({token}): request failed — {e}")
                return []
        except requests.RequestException as e:
            print(f"[lever] {company} ({token}): request failed — {e}")
            return []

    if resp.status_code == 404:
        print(f"[lever] {company} ({token}): 404 — not on Lever or wrong token. Skipping.")
        return []
    if resp.status_code != 200:
        print(f"[lever] {company} ({token}): HTTP {resp.status_code}. Skipping.")
        return []

    try:
        payload = resp.json()
    except ValueError:
        print(f"[lever] {company} ({token}): response was not JSON. Skipping.")
        return []
    if not isinstance(payload, list):
        print(f"[lever] {company} ({token}): unexpected response shape. Skipping.")
        return []

    jobs = []
    for raw in payload:
        title = (raw.get("text") or "").strip()
        if not title:
            continue

        categories = raw.get("categories") or {}
        location = categories.get("location") or ", ".join(categories.get("allLocations") or [])
        if raw.get("workplaceType") == "remote" and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()

        url = raw.get("hostedUrl") or raw.get("applyUrl") or ""
        department = " / ".join(
            p for p in (categories.get("department"), categories.get("team")) if p
        )

        jobs.append({
            "job_id": make_job_id(ATS, raw.get("id"), company, title, url),
            "company": company,
            "title": title,
            "location": location,
            "ats_source": ATS,
            "url": url,
            "description_text": _description(raw),
            "published_at": _published(raw.get("createdAt")),
            "department": department,
        })

    print(f"[lever] {company} ({token}): {len(jobs)} postings")
    return jobs
