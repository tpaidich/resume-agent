"""One polling pass: fetch every registered board, filter, score, store.

This is read-only traffic against public APIs and it never applies to anything.
The pass ends with rows in applications/sourced_jobs.db waiting for review.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from . import ashby_source, greenhouse_source, job_store, sponsorship_filter
from .job_scorer import _background_summary, claude_score, keyword_match
from .paths import COMPANIES_FILE

_SOURCES = {
    "greenhouse": greenhouse_source.fetch_jobs,
    "ashby": ashby_source.fetch_jobs,
}


def load_companies(path: str | None = None) -> list[dict]:
    with open(path or COMPANIES_FILE) as f:
        entries = yaml.safe_load(f) or []

    valid = []
    for entry in entries:
        ats = str(entry.get("ats", "")).strip().lower()
        if ats not in _SOURCES:
            print(f"[pipeline] {entry.get('company')}: unsupported ats '{ats}', skipping")
            continue
        if not entry.get("token") or not entry.get("company"):
            print(f"[pipeline] entry missing company or token, skipping: {entry}")
            continue
        valid.append({**entry, "ats": ats})
    return valid


def fetch_all(companies: list[dict], max_workers: int = 6) -> list[dict]:
    """Fetch every board concurrently. A failing board yields an empty list."""
    postings = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_SOURCES[c["ats"]], c["token"], c["company"]): c
            for c in companies
        }
        for future in as_completed(futures):
            company = futures[future]
            try:
                postings.extend(future.result())
            except Exception as e:
                print(f"[pipeline] {company['company']}: unexpected error — {e}")
    return postings


def run_once(score: bool = True, score_limit: int | None = None, path: str | None = None) -> dict:
    """Run a full pass. Returns a summary dict of what happened."""
    started = time.time()
    job_store.init_db()

    companies = load_companies(path)
    print(f"[pipeline] polling {len(companies)} companies")

    known_before = job_store.known_job_ids()
    postings = fetch_all(companies)
    print(f"[pipeline] fetched {len(postings)} postings")

    registry = sponsorship_filter.load_registry(path)

    inserted = updated = 0
    new_rows = []
    for posting in postings:
        status, _evidence = sponsorship_filter.assess(
            posting["company"], posting.get("description_text", ""), registry
        )
        posting["sponsorship_status"] = status
        is_new = posting["job_id"] not in known_before

        result = job_store.upsert_job(posting)
        if result == "inserted":
            inserted += 1
        else:
            updated += 1
        if is_new:
            new_rows.append(posting)

    print(f"[pipeline] {inserted} new, {updated} refreshed")

    summary = {
        "companies": len(companies),
        "fetched": len(postings),
        "inserted": inserted,
        "updated": updated,
        "keyword_passed": 0,
        "scored": 0,
        "score_failures": 0,
        "seconds": 0.0,
    }

    if not score:
        summary["seconds"] = round(time.time() - started, 1)
        return summary

    # Score anything still unscored, which is the new rows plus any earlier row
    # whose scoring call previously failed.
    pending = job_store.unscored_jobs()
    candidates = []
    for row in pending:
        passes, _families = keyword_match(row["title"], row["description_text"])
        if passes:
            candidates.append(row)

    summary["keyword_passed"] = len(candidates)
    print(
        f"[pipeline] {len(pending)} unscored; {len(candidates)} passed the keyword gate "
        f"({len(pending) - len(candidates)} skipped before any Claude call)"
    )

    if score_limit:
        candidates = candidates[:score_limit]

    if candidates:
        background = _background_summary()
        for row in candidates:
            value, reason = claude_score(row, background)
            if value is None:
                summary["score_failures"] += 1
                continue
            job_store.set_scoring(row["job_id"], value, reason)
            summary["scored"] += 1
            print(f"[pipeline] {value}/10  {row['company']} — {row['title'][:60]}")

    summary["seconds"] = round(time.time() - started, 1)
    print(
        f"[pipeline] done in {summary['seconds']}s — "
        f"{summary['scored']} scored, {summary['score_failures']} failed"
    )
    return summary
