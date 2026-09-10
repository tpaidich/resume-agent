"""Score sourced postings for fit.

Two stages, cheap first:

1. A keyword pass over title and description against the target role families.
   A posting with no overlap at all is rejected here and never costs a Claude
   call. On a poll that pulls several thousand postings this is the difference
   between a handful of API calls and a few thousand.
2. For survivors, one Claude call returning a 1-10 fit score and a one-sentence
   reason as strict JSON.

Generic terms are only matched against the title. Matching a word like
"strategy" anywhere in a 10,000-character job description would let essentially
every posting through and defeat the point of the gate. Distinctive multi-word
phrases ("forward deployed", "chief of staff") are matched against both.
"""
import json
import re

import anthropic
import yaml

from .paths import MASTER_RESUME

_client = None

MODEL = "claude-haiku-4-5-20251001"

# Target role families. `title_terms` are generic enough that they only count
# in a title; `strong_terms` are distinctive enough to count anywhere.
ROLE_FAMILIES = {
    "Chief of Staff": {
        "strong_terms": ["chief of staff", "business operations", "biz ops",
                          "strategy and operations", "strategy & operations",
                          "strategic operations"],
        "title_terms": ["chief of staff", "bizops", "strategy", "operations strategy"],
    },
    "Forward Deployed Engineer": {
        "strong_terms": ["forward deployed", "forward-deployed", "embedded engineer"],
        "title_terms": ["forward deployed engineer", "fde", "solutions engineer",
                         "solutions architect", "implementation engineer",
                         "field engineer", "customer engineer"],
    },
    "Deployment Strategist": {
        "strong_terms": ["deployment strategist", "technical implementation"],
        "title_terms": ["deployment strategist", "deployment engineer",
                         "implementation specialist", "implementation consultant",
                         "delivery strategist"],
    },
    "AI Strategist": {
        "strong_terms": ["ai strategist", "ai strategy", "ai transformation",
                          "applied ai", "ai adoption", "ai enablement"],
        "title_terms": ["ai strategist", "ai solutions", "ai consultant",
                         "ai program manager", "ai product", "applied ai"],
    },
    "GTM Engineer": {
        "strong_terms": ["gtm engineer", "go-to-market engineer",
                          "go to market engineer"],
        "title_terms": ["gtm engineer", "growth engineer", "sales engineer",
                         "technical account manager", "partner engineer",
                         "revenue operations", "solutions consultant"],
    },
}

# Titles that are themselves targets and must survive the seniority rules
# below. "Chief of Staff" contains both "chief" and "staff", either of which
# would otherwise reject it, so it is removed before the seniority check runs.
_SENIORITY_EXEMPT = re.compile(r"chief of staff", re.IGNORECASE)

# Seniority the candidate (roughly 1-2 years of experience) cannot reach.
_SENIORITY_BLOCK = [
    r"\bsenior\b", r"\bsr\.?\b", r"\bstaff\b", r"\bprincipal\b", r"\blead\b",
    r"\bdirector\b", r"\bvp\b", r"\bvice president\b", r"\bhead of\b",
    r"\bexecutive director\b", r"\bmanaging director\b",
    r"\bchief\b", r"\bcto\b", r"\bcoo\b", r"\bcfo\b", r"\bcio\b",
    r"\bfellow\b", r"\bdistinguished\b",
]
_SENIORITY_RE = re.compile("|".join(_SENIORITY_BLOCK), re.IGNORECASE)

# Roles that are not a fit regardless of level.
_FUNCTION_BLOCK = re.compile(
    r"\b(?:recruiter|recruiting|talent acquisition|human resources|"
    r"payroll|paralegal|attorney|counsel|nurse|technician|welder|"
    r"machinist|driver|warehouse|janitor|security guard|barista|"
    r"line cook|maintenance|electrician|forklift)\b",
    re.IGNORECASE,
)


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def keyword_match(title: str, description_text: str) -> tuple[bool, list[str]]:
    """Return (passes, matched_family_names).

    Rejects on seniority and clearly off-function roles first, then requires at
    least one role-family signal.
    """
    title_l = (title or "").lower()
    desc_l = (description_text or "").lower()

    seniority_probe = _SENIORITY_EXEMPT.sub(" ", title_l)
    if _SENIORITY_RE.search(seniority_probe) or _FUNCTION_BLOCK.search(title_l):
        return False, []

    matched = []
    for family, terms in ROLE_FAMILIES.items():
        hit = any(t in title_l for t in terms["title_terms"]) or any(
            t in title_l or t in desc_l for t in terms["strong_terms"]
        )
        if hit:
            matched.append(family)

    return bool(matched), matched


def _background_summary(master: dict | None = None) -> str:
    """Condense master.yaml to the top-level experience entries.

    The full file (every bullet, all coursework) is far more than the scorer
    needs and makes each call slower and more expensive.
    """
    if master is None:
        with open(MASTER_RESUME) as f:
            master = yaml.safe_load(f)

    lines = []
    meta = master.get("meta", {})
    if meta.get("work_authorization"):
        lines.append(f"Work authorization: {meta['work_authorization']}")

    for edu in master.get("education", []):
        lines.append(
            f"Education: {edu.get('degree')}, {edu.get('institution')} "
            f"(conferred {edu.get('graduation')})"
        )

    skills = master.get("skills", {})
    for category, items in skills.items():
        lines.append(f"{category}: {', '.join(items)}")

    lines.append("Experience:")
    for exp in master.get("experience", []):
        domain = ", ".join(exp.get("domain", []))
        tools = ", ".join(exp.get("tools", []))
        lines.append(
            f"  - {exp.get('role')}, {exp.get('company')} "
            f"({exp.get('start')} to {exp.get('end')})"
            + (f"; focus: {domain}" if domain else "")
            + (f"; tools: {tools}" if tools else "")
        )

    return "\n".join(lines)


_PROMPT = """You are a recruiter screening roles for this candidate.

CANDIDATE BACKGROUND:
{background}

The candidate has roughly 1-2 years of professional experience (degree conferred
December 2025) and is targeting these role families: Chief of Staff, Forward
Deployed Engineer, Deployment Strategist, AI Strategist, GTM Engineer.

JOB: {title} at {company}
LOCATION: {location}
DESCRIPTION:
{description}

Rate fit from 1 to 10, where 10 means the candidate clearly meets the bar and
1 means clearly wrong. A posting demanding materially more experience than the
candidate has should score low no matter how well the skills line up.

Return strict JSON only. No prose, no markdown fences:
{{"score": <integer 1-10>, "reason": "<one sentence, under 25 words>"}}"""


def claude_score(job: dict, background: str, model: str = MODEL) -> tuple[int | None, str | None]:
    """Return (score, reason). Returns (None, None) on failure so the row stays
    unscored and gets retried on the next poll rather than being written off."""
    prompt = _PROMPT.format(
        background=background,
        title=job.get("title", ""),
        company=job.get("company", ""),
        location=job.get("location") or "not stated",
        description=(job.get("description_text") or "")[:4000],
    )

    try:
        msg = _get_client().messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        start, end = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[start:end])
        score = int(data["score"])
        score = max(1, min(10, score))
        reason = str(data.get("reason", "")).strip()[:300]
        return score, reason
    except Exception as e:
        print(f"[scorer] {job.get('company')} — {job.get('title')}: scoring failed ({e})")
        return None, None


def score_job(job: dict, background: str | None = None) -> dict:
    """Score one posting. Returns {'score', 'reason', 'families', 'skipped'}."""
    passes, families = keyword_match(job.get("title", ""), job.get("description_text", ""))
    if not passes:
        return {"score": None, "reason": None, "families": [], "skipped": True}

    background = background if background is not None else _background_summary()
    score, reason = claude_score(job, background)
    return {"score": score, "reason": reason, "families": families, "skipped": False}
