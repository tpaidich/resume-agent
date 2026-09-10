"""
Autonomous job finder. Searches Remotive (free, no key) and targeted
Greenhouse company boards. Add a RAPIDAPI_KEY env var to also pull from
JSearch (LinkedIn/Indeed/Glassdoor aggregate — 500 free calls/month).
"""
import os
import re
import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ResumeAgent/1.0)"}

# Title terms that immediately disqualify a job
BLOCK_TITLE_TERMS = [
    "senior", "sr.", "sr ", "lead ", "principal", "staff ",
    "director", "vp ", "vice president", "head of", "chief ",
    "c-suite", "cto", "cdo", "coo",
    # wrong functions
    "sales", "account executive", "account manager", "marketing",
    "recruiter", "recruiting", "human resources", "hr ", "legal",
    "finance manager", "controller", "accountant",
]

# Title must contain at least one of these to be considered relevant
REQUIRED_TITLE_TERMS = [
    "data", "analyt", "scientist", "engineer", "intelligence",
    "consultant", "project", "program", "manager", "tpm",
    "machine learning", " ml ", "ai ", "insight", "reporting",
]


def filter_jobs(jobs: list[dict]) -> list[dict]:
    """Drop senior roles and anything clearly off-target."""
    out = []
    for job in jobs:
        title_lower = job.get("title", "").lower()
        if any(b in title_lower for b in BLOCK_TITLE_TERMS):
            continue
        if not any(r in title_lower for r in REQUIRED_TITLE_TERMS):
            continue
        out.append(job)
    return out


# Keywords per category
ROLE_KEYWORDS = {
    "Data Science":        ["data scientist", "data science", "machine learning engineer", "ML engineer"],
    "Data Analytics":      ["data analyst", "analytics engineer", "quantitative analyst"],
    "Business Analytics":  ["business analyst", "business intelligence", "BI analyst", "business analytics"],
    "Project Analyst":     ["project analyst", "program analyst", "strategy analyst"],
    "Project Manager":     ["project manager", "program manager", "technical program manager", "TPM"],
    "Consulting":          ["consultant", "strategy consultant", "management consultant", "associate consultant"],
    "Tech Consulting":     ["technology consultant", "tech consultant", "solutions consultant", "implementation consultant"],
    "Data Engineering":    ["data engineer", "analytics engineer", "data platform engineer"],
}

# Greenhouse company slugs to scan
GREENHOUSE_COMPANIES = [
    "anthropic", "stripe", "airbnb", "figma", "notion",
    "databricks", "snowflake", "doordash", "brex", "plaid",
    "asana", "hubspot", "datadog", "amplitude", "fivetran",
    "scale-ai", "cohere", "replit", "airtable", "retool",
]


def find_jobs(categories: list[str], max_per_source: int = 30) -> list[dict]:
    """Return deduplicated job list across all sources for the given categories."""
    keywords = []
    for cat in categories:
        keywords.extend(ROLE_KEYWORDS.get(cat, [cat.lower()]))
    keywords = list(dict.fromkeys(k.lower() for k in keywords))  # dedupe, preserve order

    results = []
    results.extend(_search_remotive(keywords, max_per_source))
    results.extend(_search_greenhouse(keywords, max_per_source))

    rapidapi_key = os.getenv("RAPIDAPI_KEY")
    if rapidapi_key:
        results.extend(_search_jsearch(keywords, rapidapi_key, max_per_source))

    # Deduplicate by URL
    seen, unique = set(), []
    for job in results:
        if job["url"] not in seen:
            seen.add(job["url"])
            unique.append(job)

    return unique


# ---------------------------------------------------------------------------
# Source 1: Remotive (free, no key, remote jobs)
# ---------------------------------------------------------------------------
def _search_remotive(keywords: list[str], limit: int) -> list[dict]:
    results = []
    seen_ids = set()
    for kw in keywords[:6]:  # limit API calls
        try:
            resp = requests.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": kw, "limit": limit},
                headers=_HEADERS,
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            for job in resp.json().get("jobs", []):
                if job["id"] in seen_ids:
                    continue
                seen_ids.add(job["id"])
                results.append({
                    "title":   job.get("title", ""),
                    "company": job.get("company_name", "Unknown"),
                    "url":     job.get("url", ""),
                    "source":  "remotive",
                })
        except Exception:
            continue
    return results


# ---------------------------------------------------------------------------
# Source 2: Greenhouse public boards (targeted company list)
# ---------------------------------------------------------------------------
def _fetch_greenhouse_company(slug: str, keywords: list[str]) -> list[dict]:
    try:
        resp = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
            headers=_HEADERS, timeout=6,
        )
        if resp.status_code != 200:
            return []
        company_name = slug.replace("-", " ").title()
        return [
            {
                "title":   job.get("title", ""),
                "company": company_name,
                "url":     f"https://boards.greenhouse.io/{slug}/jobs/{job['id']}",
                "source":  "greenhouse",
            }
            for job in resp.json().get("jobs", [])
            if any(kw in job.get("title", "").lower() for kw in keywords)
        ]
    except Exception:
        return []


def _search_greenhouse(keywords: list[str], limit: int) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_greenhouse_company, slug, keywords) for slug in GREENHOUSE_COMPANIES]
        for f in as_completed(futures, timeout=20):
            try:
                results.extend(f.result())
            except Exception:
                pass
    return results


# ---------------------------------------------------------------------------
# Source 3: JSearch via RapidAPI (LinkedIn/Indeed/Glassdoor aggregate)
#           Set RAPIDAPI_KEY env var to enable. Free tier: 500 calls/month.
# ---------------------------------------------------------------------------
def _search_jsearch(keywords: list[str], api_key: str, limit: int) -> list[dict]:
    results = []
    for kw in keywords[:4]:
        try:
            resp = requests.get(
                "https://jsearch.p.rapidapi.com/search",
                headers={
                    "X-RapidAPI-Key":  api_key,
                    "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
                },
                params={"query": kw, "num_pages": "1", "page": "1"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            for job in resp.json().get("data", [])[:limit]:
                url = job.get("job_apply_link") or job.get("job_google_link", "")
                results.append({
                    "title":   job.get("job_title", ""),
                    "company": job.get("employer_name", "Unknown"),
                    "url":     url,
                    "source":  "jsearch",
                })
        except Exception:
            continue
    return results
