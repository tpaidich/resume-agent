"""One polling pass: fetch every registered board, filter, score, store.

This is read-only traffic against public APIs and it never applies to anything.
The pass ends with rows in applications/sourced_jobs.db waiting for review.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import ashby_source, company_store, greenhouse_source, job_store, lever_source, sponsorship_filter
from .job_scorer import _background_summary, claude_score, keyword_match

_SOURCES = {
    "greenhouse": greenhouse_source.fetch_jobs,
    "ashby": ashby_source.fetch_jobs,
    "lever": lever_source.fetch_jobs,
}

# A board whose last poll came back empty is polled weekly instead of every
# pass. Most discovered boards are not hiring on any given day.
DEAD_BOARD_RECHECK_DAYS = 7


def load_companies(path: str | None = None) -> list[dict]:
    """Boards to poll, from the companies table.

    companies.yaml is folded in on every call, so a hand-added entry is picked
    up without running discovery. See tools/sourcing/discover_companies.py for
    how the rest of the table fills.
    """
    company_store.init_db()
    added = company_store.seed_from_yaml(path)
    if added:
        print(f"[pipeline] added {added} companies from companies.yaml")

    return [
        {"company": row["name"] or row["slug"], "token": row["slug"], "ats": row["platform"]}
        for row in company_store.pollable_companies(DEAD_BOARD_RECHECK_DAYS)
    ]


def fetch_all(companies: list[dict], max_workers: int = 6) -> list[dict]:
    """Fetch every board concurrently. A failing board yields an empty list.

    Each board's result is written back to the companies table. A request
    failure also reads as empty, which only defers that board to next week.
    """
    postings = []
    checked = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_SOURCES[c["ats"]], c["token"], c["company"]): c
            for c in companies
        }
        for future in as_completed(futures):
            company = futures[future]
            try:
                jobs = future.result()
            except Exception as e:
                print(f"[pipeline] {company['company']}: unexpected error — {e}")
                continue
            postings.extend(jobs)
            checked.append((company["token"], company["ats"], bool(jobs)))
    company_store.mark_checked(checked)
    return postings


# Only postings published within this many days are stored and scored. Boards
# carry openings that have sat for months, and every one scored is a Claude call.
MAX_POSTING_AGE_DAYS = 7


def run_once(
    score: bool = True,
    score_limit: int | None = None,
    path: str | None = None,
    max_age_days: float | None = MAX_POSTING_AGE_DAYS,
) -> dict:
    """Run a full pass. Returns a summary dict of what happened.

    max_age_days=None stores and scores postings of any age.
    """
    started = time.time()
    job_store.init_db()

    companies = load_companies(path)
    print(f"[pipeline] polling {len(companies)} companies")

    known_before = job_store.known_job_ids()
    postings = fetch_all(companies)
    print(f"[pipeline] fetched {len(postings)} postings")
    if max_age_days is not None:
        postings = [p for p in postings if job_store.published_within(p.get("published_at"), max_age_days)]
        print(f"[pipeline] {len(postings)} published in the last {max_age_days:g} days")

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
    if max_age_days is not None:
        # Rows stored before the age cutoff existed can be months old.
        pending = [r for r in pending if job_store.published_within(r["published_at"], max_age_days)]
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
