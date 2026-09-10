import re
import requests
import anthropic
from bs4 import BeautifulSoup
from urllib.parse import urlparse

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ResumeAgent/1.0)"}

# Common job board domains where the domain name is NOT the company
_JOB_BOARD_DOMAINS = {
    "greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com",
    "icims.com", "taleo.net", "smartrecruiters.com", "jobvite.com",
    "ashbyhq.com", "rippling.com", "bamboohr.com", "workable.com",
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "embed.com", "breezy.hr", "pinpoint.com", "recruitee.com",
    "join.com", "apply.com", "jobs.com", "careers.com",
}

# Domain first-segments that are clearly not company names
_SUSPICIOUS_NAMES = {
    "unknown", "embed", "jobs", "careers", "apply", "hire", "work",
    "talent", "recruit", "join", "portal", "staffing",
}


def _extract_company_from_description(description: str, title: str) -> str | None:
    """Extract company name from JD. Tries regex first; falls back to Haiku only if needed."""
    text = description[:2000]
    patterns = [
        r"(?:^|\n)\s*About\s+([A-Z][A-Za-z0-9\s&\.\,'\-]{2,50}?)[\s\n:]",
        r"(?:^|\n)\s*([A-Z][A-Za-z0-9\s&\.\,'\-]{2,40}?)\s+is (?:a |an |the )",
        r"(?:join|working at|work at|joining)\s+([A-Z][A-Za-z0-9\s&\.\,'\-]{2,40}?)[\s,\.]",
        r"([A-Z][A-Za-z0-9\s&\.\,'\-]{2,40}?)\s+(?:is hiring|is looking|seeks|is seeking)",
        r"(?:at|@)\s+([A-Z][A-Za-z0-9\s&\.\,'\-]{2,40}?)[\s,\.\n]",
        r"(?:^|\n)\s*([A-Z][A-Za-z0-9\s&\.\,'\-]{2,40}?)\s+(?:LLC|Inc\.|Corp\.|Ltd\.|LLP)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            candidate = m.group(1).strip().rstrip(",.")
            if len(candidate) >= 3 and candidate.lower() not in _SUSPICIOUS_NAMES:
                return candidate

    # Regex failed — ask Haiku (cheap, last resort only)
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": (
                f"Job title: {title}\n\nJob description (first 1500 chars):\n{description[:1500]}\n\n"
                "What is the name of the company that posted this job? "
                "Reply with ONLY the company name, nothing else. If truly unknown, reply 'Unknown'."
            )}],
        )
        result = msg.content[0].text.strip().strip('"').strip("'")
        return result if result.lower() != "unknown" else None
    except Exception:
        return None


def scrape_job(url: str) -> dict:
    if "greenhouse.io" in url:
        return _parse_greenhouse(url)
    if "lever.co" in url:
        return _parse_lever(url)
    return _parse_generic(url)


def _company_from_title(title: str) -> str | None:
    """Extract company from title patterns like 'Role @ Company' or 'Role at Company'."""
    m = re.search(r"\s[@|]\s+(.+)$", title) or re.search(r"\s+at\s+([A-Z][^\s,]+(?:\s[A-Z][^\s,]+)*)\s*$", title)
    if m:
        return m.group(1).strip()
    return None


def _company_from_url(url: str) -> str | None:
    """Infer company name from domain when not on a job board."""
    host = urlparse(url).hostname or ""
    # Strip www. and known prefixes
    host = re.sub(r"^(www\d?|jobs|careers|work)\.", "", host)
    # Check it's not a job board
    for board in _JOB_BOARD_DOMAINS:
        if board in host:
            return None
    # Take the first domain segment (e.g. 'openai' from 'openai.com')
    name = host.split(".")[0]
    return name.replace("-", " ").title() if name else None


def _parse_greenhouse(url: str) -> dict:
    m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if m:
        company_slug, job_id = m.group(1), m.group(2)
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs/{job_id}?questions=false"
        resp = requests.get(api_url, timeout=8, headers=_HEADERS)
        if resp.status_code == 200:
            data = resp.json()
            company = company_slug.replace("-", " ").title()
            description_html = data.get("content", "") or ""
            description = BeautifulSoup(description_html, "html.parser").get_text(separator="\n", strip=True)
            title = data.get("title", "Unknown Role")
            # Company in title takes precedence (e.g. "Role @ OpenAI")
            company = _company_from_title(title) or company
            return {
                "title": title,
                "company": company,
                "url": url,
                "description": description,
                "source": "greenhouse-api",
            }
        if resp.status_code == 404:
            raise ValueError(f"Job not found (404) — it may have been closed: {url}")

    resp = requests.get(url, timeout=8, headers=_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown Role"
    content = soup.find("div", id="content") or soup.find("div", class_="job-description")
    description = content.get_text(separator="\n", strip=True) if content else soup.get_text(separator="\n", strip=True)[:5000]
    m2 = re.search(r"greenhouse\.io/([^/]+)/", url)
    company = m2.group(1).replace("-", " ").title() if m2 else "Unknown"
    company = _company_from_title(title) or company
    return {"title": title, "company": company, "url": url, "description": description, "source": "greenhouse-html"}


def _parse_lever(url: str) -> dict:
    resp = requests.get(url, timeout=8, headers=_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title_tag = soup.find("h2") or soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown Role"
    company_tag = soup.find("a", class_="main-header-logo")
    company = company_tag.get_text(strip=True) if company_tag else None
    company = company or _company_from_title(title) or _company_from_url(url)
    content = soup.find("div", class_="section-wrapper")
    description = content.get_text(separator="\n", strip=True) if content else ""
    company = _resolve_company(company, description, title)
    return {"title": title, "company": company, "url": url, "description": description, "source": "lever"}


def _resolve_company(company: str | None, description: str, title: str) -> str:
    """If company is missing or suspicious, ask Claude to extract it from the description."""
    if company and company.lower() not in _SUSPICIOUS_NAMES:
        return company
    extracted = _extract_company_from_description(description, title)
    return extracted or company or "Unknown"


def _parse_generic(url: str) -> dict:
    resp = requests.get(url, timeout=8, headers=_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown Role"
    description = soup.get_text(separator="\n", strip=True)

    company = _company_from_title(title)
    if not company:
        meta_site = soup.find("meta", property="og:site_name") or soup.find("meta", attrs={"name": "application-name"})
        if meta_site:
            company = meta_site.get("content", "").strip() or None
    if not company:
        company = _company_from_url(url)

    company = _resolve_company(company, description, title)
    return {"title": title, "company": company, "url": url, "description": description, "source": "generic"}
