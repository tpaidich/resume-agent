import json
import os
import anthropic

client = anthropic.Anthropic()

CACHE_FILE = "applications/score_cache.json"

LOCATION_NOTE = (
    "Candidate is open to ALL locations — remote, hybrid, or on-site anywhere. "
    "Never flag location or relocation as a gap. "
    "Candidate is available to start immediately. "
    "Candidate's B.S. was conferred December 2025 and is fully in hand — never flag degree completion as a gap."
)

_SPONSORSHIP_CLAUSE = (
    "Also check whether the job posting explicitly states it does NOT offer visa sponsorship "
    "(phrases like 'must be authorized to work', 'no sponsorship', 'cannot sponsor', "
    "'should not require sponsorship'). "
    "The candidate is on OPT and requires sponsorship. "
    "Set excludes_sponsorship to true ONLY if the job explicitly rules it out."
)

# Calibration rules injected into every scoring prompt
_CALIBRATION = """SCORING CALIBRATION — read carefully before assigning a score:

The candidate has approximately 1–2 years of professional experience total (graduated December 2025).

Score thresholds:
- 80–100: Strong match. Candidate meets nearly all requirements including any stated years-of-experience bar.
- 60–79:  Good match. Candidate meets most requirements; minor gaps only.
- 40–59:  Partial match. Meaningful gaps exist but the role is still reachable.
- 0–39:   Weak match. Major unmet requirements make the candidate clearly underqualified.

Hard penalties (apply before finalizing the score):
- Job requires 3+ years of experience in any area → candidate's 1–2 years is a major gap → subtract at least 20 points.
- Job requires 5+ years → subtract at least 35 points.
- Job requires a specific degree, certification, or clearance the candidate does not have → subtract at least 15 points.
- Multiple hard gaps stack — do not cap the deductions.

Do NOT inflate the score because the candidate's skills partially match. A strong skills match does not compensate for a large experience shortfall when the posting sets a hard bar."""


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    os.makedirs("applications", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


# ---------------------------------------------------------------------------
# Single-job score (used by manual Run button and CLI agent)
# ---------------------------------------------------------------------------

def score_fit(job: dict, master: dict) -> dict:
    """Score a single job. Checks URL cache first; uses Haiku."""
    url = job.get("url", "")
    if url:
        cache = _load_cache()
        if url in cache:
            return cache[url]

    resume_text = _build_resume_text(master)
    prompt = f"""You are a senior technical recruiter. Score how well this candidate fits the job.
Be precise — only flag something as a gap if it is genuinely absent from the resume text below.

LOCATION NOTE: {LOCATION_NOTE}

{_CALIBRATION}

JOB: {job['title']} at {job['company']}
DESCRIPTION:
{job['description'][:3000]}

FULL RESUME:
{resume_text}

{_SPONSORSHIP_CLAUSE}

Return JSON only — no prose, no markdown fences:
{{
  "score": <integer 0-100>,
  "excludes_sponsorship": <true or false>,
  "reasons": ["<one-liner>", ...],
  "strengths": ["<specific matched skill or experience>", ...],
  "gaps": ["<only genuinely missing requirement>", ...]
}}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        result = json.loads(text[start:end])
    except Exception:
        result = {"score": 0, "excludes_sponsorship": False, "reasons": ["parse error"], "strengths": [], "gaps": []}

    if url:
        cache = _load_cache()
        cache[url] = result
        _save_cache(cache)

    return result


# ---------------------------------------------------------------------------
# Batch scoring — one Claude call for N jobs (used by Find Jobs flow)
# ---------------------------------------------------------------------------

def score_jobs_batch(jobs: list[dict], master: dict) -> list[dict]:
    """Score multiple jobs in a single Haiku call. Returns list in same order as input."""
    if not jobs:
        return []

    cache = _load_cache()
    results: list[dict | None] = [None] * len(jobs)
    uncached: list[int] = []

    for i, job in enumerate(jobs):
        url = job.get("url", "")
        if url and url in cache:
            results[i] = cache[url]
        else:
            uncached.append(i)

    if not uncached:
        return results  # type: ignore[return-value]

    resume_text = _build_resume_text(master)

    jobs_block = "\n\n".join(
        f"JOB {seq}: {jobs[i]['title']} at {jobs[i]['company']}\n"
        f"{jobs[i].get('description', '')[:600]}"
        for seq, i in enumerate(uncached)
    )

    prompt = f"""You are a senior technical recruiter. Score each job for this candidate.
Be precise — only flag a gap if it is genuinely absent from the resume.

LOCATION NOTE: {LOCATION_NOTE}

{_CALIBRATION}

CANDIDATE RESUME:
{resume_text}

JOBS TO SCORE (index 0 through {len(uncached) - 1}):
{jobs_block}

{_SPONSORSHIP_CLAUSE}

Return a JSON array only — no prose, no markdown fences. One object per job in order:
[
  {{"index": 0, "score": <0-100>, "excludes_sponsorship": <bool>, "strengths": ["..."], "gaps": ["..."]}},
  ...
]"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text
    try:
        start, end = text.find("["), text.rfind("]") + 1
        batch = json.loads(text[start:end])
    except Exception:
        batch = []

    updated_cache = False
    for item in batch:
        seq = item.get("index")
        if seq is None or seq >= len(uncached):
            continue
        job_index = uncached[seq]
        result = {
            "score":                item.get("score", 0),
            "excludes_sponsorship": item.get("excludes_sponsorship", False),
            "strengths":            item.get("strengths", []),
            "gaps":                 item.get("gaps", []),
        }
        results[job_index] = result
        url = jobs[job_index].get("url", "")
        if url:
            cache[url] = result
            updated_cache = True

    if updated_cache:
        _save_cache(cache)

    fallback = {"score": 0, "excludes_sponsorship": False, "strengths": [], "gaps": []}
    return [r if r is not None else fallback for r in results]


# ---------------------------------------------------------------------------
# Resume text builder
# ---------------------------------------------------------------------------

def _build_resume_text(master: dict) -> str:
    lines = []

    meta = master.get("meta", {})
    if meta.get("name"):
        lines.append(f"Name: {meta['name']}")
    if meta.get("location"):
        lines.append(f"Location: {meta['location']}")
    if meta.get("work_authorization"):
        lines.append(f"Work authorization: {meta['work_authorization']}")

    for edu in master.get("education", []):
        lines.append(
            f"\nEducation: {edu.get('degree')} — {edu.get('institution')} "
            f"(conferred {edu.get('graduation')}, degree in hand)"
        )
        if edu.get("minor"):
            lines.append(f"  Minor: {edu['minor']}")

    skills = master.get("skills", {})
    if skills:
        lines.append("\nSkills:")
        for category, items in skills.items():
            lines.append(f"  {category}: {', '.join(items)}")

    for cert in master.get("certifications", []):
        lines.append(f"Certification: {cert.get('name')} ({cert.get('issuer')})")

    coursework = master.get("coursework", [])
    if coursework:
        lines.append(f"\nCoursework: {', '.join(coursework)}")

    lines.append("\nExperience:")
    for exp in master.get("experience", []):
        lines.append(
            f"\n  {exp.get('role')} at {exp.get('company')} "
            f"({exp.get('start')} – {exp.get('end')})"
        )
        lines.append(f"  Domain: {', '.join(exp.get('domain', []))}")
        lines.append(f"  Tools: {', '.join(exp.get('tools', []))}")
        for b in exp.get("bullets", []):
            lines.append(f"    • {b}")

    return "\n".join(lines)
