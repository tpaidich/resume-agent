"""Determine a posting's sponsorship status.

Two layers, in order:

1. Company-level lookup from config/companies.yaml. If the registry says `yes`
   or `no`, that is the answer and no text scan runs.
2. A scan of the job description, only when the company status is `unknown`.

The scan can only ever produce `flagged_no_sponsorship_in_jd`. It never
produces `yes`: the absence of a disclaimer is not evidence that a company
sponsors, it only means nothing ruled it out automatically. An unknown stays
unknown.

Nothing here drops a posting. Both `unknown` and `flagged_no_sponsorship_in_jd`
rows still reach the review queue, labeled, because a one-line manual check is
cheap and a wrongly auto-excluded good match is not.

Phrase matching is deliberately narrow. In real postings "sponsor" overwhelm-
ingly means something else ("executive sponsor", "event sponsorships",
"sponsor relationships", "we will sponsor you for relocation"), so matching the
bare word would flag a large share of perfectly eligible roles.
"""
import re

import yaml

from .paths import COMPANIES_FILE

STATUS_YES = "yes"
STATUS_NO = "no"
STATUS_UNKNOWN = "unknown"
STATUS_FLAGGED = "flagged_no_sponsorship_in_jd"

# Each pattern must be specific enough that a match is genuinely a disclaimer.
_DISCLAIMER_PATTERNS = [
    # Direct refusals to sponsor.
    r"\b(?:un(?:able|willing)|not able|do(?:es)? not have the ability)\s+to\s+sponsor\b",
    r"\b(?:we|the company|this employer)\s+(?:do(?:es)?\s+not|will not|cannot|can'?t)\s+"
    r"(?:currently\s+|at this time\s+)?(?:offer|provide|sponsor)\b[^.\n]{0,60}"
    r"(?:sponsor|visa|immigration)",
    r"\b(?:visa\s+)?sponsorship\s+is\s+not\s+(?:available|offered|provided|possible)\b",
    r"\bno\s+(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|offered|provided)?\b",
    r"\bdoes\s+not\s+(?:currently\s+)?(?:offer|provide)\s+(?:visa\s+)?sponsorship\b",
    r"\bnot\s+(?:currently\s+)?(?:hiring|considering)\s+[^.\n]{0,80}sponsorship\b",
    r"\bsponsor\s+or\s+take\s+over\s+sponsorship\b",
    r"\bwill\s+not\s+sponsor\b",

    # "Authorized to work ..." only counts with a qualifier that rules out
    # future sponsorship. Bare work-authorization language does not, since an
    # OPT holder is in fact authorized to work today.
    r"\bauthoriz(?:ed|ation)\s+to\s+work\b[^.\n]{0,120}\bwithout\s+"
    r"(?:the\s+need\s+for\s+)?(?:visa\s+|employer\s+|current\s+or\s+future\s+)?sponsorship\b",
    r"\bwithout\s+(?:the\s+need\s+for\s+)?(?:visa\s+|employer\s+)?sponsorship\b",
    r"\bauthoriz(?:ed|ation)\s+to\s+work\b[^.\n]{0,120}\b(?:now\s+or\s+in\s+the\s+future|"
    r"on\s+a\s+permanent\s+basis|for\s+any\s+employer)\b",
    r"\bdo(?:es)?\s+not\s+(?:now\s+or\s+in\s+the\s+future\s+)?require\s+"
    r"[^.\n]{0,40}sponsorship\b",
    r"\bmust\s+not\s+require\s+[^.\n]{0,40}sponsorship\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _DISCLAIMER_PATTERNS]

# Benign senses of "sponsor" that must never be read as an immigration
# disclaimer, even if a pattern above brushes against them.
_BENIGN = re.compile(
    r"\b(?:executive|exec|corporate|event|program|research|title|team|project|"
    r"account|deal|economic)\s+sponsor(?:s|ship|ed)?\b"
    r"|\bsponsor\s+(?:relationship|you\s+for\s+relocation)"
    r"|\bgovernment[-\s]sponsored\b"
    r"|\bsponsorship(?:s)?\s+(?:of\s+)?(?:events|concerts|conferences|programs)\b",
    re.IGNORECASE,
)


def load_registry(path: str | None = None) -> dict[str, str]:
    """Map company name -> declared sponsorship status, lowercased keys."""
    with open(path or COMPANIES_FILE) as f:
        entries = yaml.safe_load(f) or []
    registry = {}
    for entry in entries:
        name = entry.get("company")
        if not name:
            continue
        status = str(entry.get("sponsorship", STATUS_UNKNOWN)).strip().lower()
        if status not in (STATUS_YES, STATUS_NO, STATUS_UNKNOWN):
            print(f"[sponsorship] {name}: unrecognized value '{status}', treating as unknown")
            status = STATUS_UNKNOWN
        registry[name.strip().lower()] = status
    return registry


def scan_description(description_text: str) -> tuple[bool, str | None]:
    """Look for a no-sponsorship disclaimer.

    Returns (found, evidence_snippet). The snippet is kept so the dashboard can
    show why a posting was flagged instead of asking the user to trust a label.
    """
    if not description_text:
        return False, None

    for pattern in _COMPILED:
        for match in pattern.finditer(description_text):
            start = max(0, match.start() - 90)
            end = min(len(description_text), match.end() + 90)
            snippet = re.sub(r"\s+", " ", description_text[start:end]).strip()
            if _BENIGN.search(snippet) and not re.search(
                r"visa|immigration|work authoriz|authoriz\w+ to work", snippet, re.I
            ):
                continue
            return True, snippet

    return False, None


def assess(
    company: str,
    description_text: str,
    registry: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    """Return (sponsorship_status, evidence). Never invents a status."""
    registry = load_registry() if registry is None else registry
    declared = registry.get((company or "").strip().lower(), STATUS_UNKNOWN)

    # Layer 1: a declared yes/no is final.
    if declared in (STATUS_YES, STATUS_NO):
        return declared, f"declared in companies.yaml as '{declared}'"

    # Layer 2: scan only when the company status is unknown.
    found, evidence = scan_description(description_text)
    if found:
        return STATUS_FLAGGED, evidence

    return STATUS_UNKNOWN, None
