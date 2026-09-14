"""SQLite persistence for sourced job postings.

tools/tracker.py stores applications in applications/tracker.json, so there is
no existing SQLite database to extend. This module owns its own file,
applications/sourced_jobs.db, and stays out of the tracker's way.

The central rule here: a re-fetch from an ATS refreshes source-of-truth fields
(title, location, description, ...) but never touches review_status or
resume_pdf_path. Those are the user's decisions, not the board's.
"""
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .paths import APPLICATIONS_DIR, DB_FILE

REVIEW_STATUSES = ("new", "interested", "rejected", "resume_generated")

SPONSORSHIP_STATUSES = ("yes", "no", "unknown", "flagged_no_sponsorship_in_jd")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sourced_jobs (
    job_id              TEXT PRIMARY KEY,
    company             TEXT NOT NULL,
    title               TEXT NOT NULL,
    location            TEXT,
    ats_source          TEXT NOT NULL,
    url                 TEXT,
    description_text    TEXT,
    published_at        TEXT,
    first_seen_at       TEXT NOT NULL,
    sponsorship_status  TEXT NOT NULL DEFAULT 'unknown',
    match_score         INTEGER,
    match_reason        TEXT,
    review_status       TEXT NOT NULL DEFAULT 'new',
    resume_pdf_path     TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON sourced_jobs (review_status);
CREATE INDEX IF NOT EXISTS idx_match_score   ON sourced_jobs (match_score DESC);
CREATE INDEX IF NOT EXISTS idx_company       ON sourced_jobs (company);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def published_within(published_at: str | None, days: float) -> bool:
    """True if an ATS publish timestamp falls within the last `days` days.

    A posting with no date, or one that cannot be parsed, counts as outside the
    window: better to skip it than to score something months old.
    """
    if not published_at:
        return False
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published >= datetime.now(timezone.utc) - timedelta(days=days)


@contextmanager
def _connect():
    os.makedirs(APPLICATIONS_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create the table and indexes if they do not exist. Safe to call often."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def make_job_id(ats: str, native_id, company: str, title: str, url: str) -> str:
    """Prefer the ATS's own id. Fall back to a hash when the board gives none."""
    if native_id not in (None, ""):
        return f"{ats}:{native_id}"
    digest = hashlib.sha256(f"{company}|{title}|{url}".encode("utf-8")).hexdigest()[:20]
    return f"{ats}:h:{digest}"


def upsert_job(job: dict) -> str:
    """Insert a posting, or refresh the ATS-owned fields of an existing one.

    Returns "inserted" or "updated". review_status and resume_pdf_path are
    never written here — they belong to the user. sponsorship_status and the
    match fields are only overwritten when the caller supplies them, so a plain
    re-fetch does not wipe an earlier scoring pass.
    """
    with _connect() as conn:
        existing = conn.execute(
            "SELECT job_id FROM sourced_jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()

        if existing is None:
            conn.execute(
                """INSERT INTO sourced_jobs
                   (job_id, company, title, location, ats_source, url,
                    description_text, published_at, first_seen_at,
                    sponsorship_status, match_score, match_reason,
                    review_status, resume_pdf_path)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'new',NULL)""",
                (
                    job["job_id"],
                    job["company"],
                    job["title"],
                    job.get("location"),
                    job["ats_source"],
                    job.get("url"),
                    job.get("description_text"),
                    job.get("published_at"),
                    _now(),
                    job.get("sponsorship_status", "unknown"),
                    job.get("match_score"),
                    job.get("match_reason"),
                ),
            )
            return "inserted"

        fields = {
            "company": job["company"],
            "title": job["title"],
            "location": job.get("location"),
            "ats_source": job["ats_source"],
            "url": job.get("url"),
            "description_text": job.get("description_text"),
            "published_at": job.get("published_at"),
        }
        for optional in ("sponsorship_status", "match_score", "match_reason"):
            if job.get(optional) is not None:
                fields[optional] = job[optional]

        assignments = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE sourced_jobs SET {assignments} WHERE job_id = ?",
            (*fields.values(), job["job_id"]),
        )
        return "updated"


def set_scoring(job_id: str, score: int | None, reason: str | None):
    with _connect() as conn:
        conn.execute(
            "UPDATE sourced_jobs SET match_score = ?, match_reason = ? WHERE job_id = ?",
            (score, reason, job_id),
        )


def set_sponsorship(job_id: str, status: str):
    if status not in SPONSORSHIP_STATUSES:
        raise ValueError(f"unknown sponsorship status: {status}")
    with _connect() as conn:
        conn.execute(
            "UPDATE sourced_jobs SET sponsorship_status = ? WHERE job_id = ?",
            (status, job_id),
        )


def set_review_status(job_id: str, status: str, resume_pdf_path: str | None = None):
    if status not in REVIEW_STATUSES:
        raise ValueError(f"unknown review status: {status}")
    with _connect() as conn:
        if resume_pdf_path is None:
            conn.execute(
                "UPDATE sourced_jobs SET review_status = ? WHERE job_id = ?",
                (status, job_id),
            )
        else:
            conn.execute(
                "UPDATE sourced_jobs SET review_status = ?, resume_pdf_path = ? WHERE job_id = ?",
                (status, resume_pdf_path, job_id),
            )


def get_job(job_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sourced_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None


def known_job_ids() -> set[str]:
    with _connect() as conn:
        return {r[0] for r in conn.execute("SELECT job_id FROM sourced_jobs")}


def query_jobs(
    review_status: str | list[str] | None = "new",
    min_score: int | None = None,
    sponsorship_status: list[str] | None = None,
    companies: list[str] | None = None,
    include_unscored: bool = True,
    max_age_days: float | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Fetch rows for the review queue, highest match_score first.

    max_age_days keeps only postings published within that many days.
    """
    clauses, params = [], []

    if review_status is not None:
        statuses = [review_status] if isinstance(review_status, str) else list(review_status)
        clauses.append(f"review_status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)

    if min_score is not None:
        if include_unscored:
            clauses.append("(match_score >= ? OR match_score IS NULL)")
        else:
            clauses.append("match_score >= ?")
        params.append(min_score)

    if sponsorship_status:
        clauses.append(f"sponsorship_status IN ({','.join('?' * len(sponsorship_status))})")
        params.extend(sponsorship_status)

    if companies:
        clauses.append(f"company IN ({','.join('?' * len(companies))})")
        params.extend(companies)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT * FROM sourced_jobs {where} "
        "ORDER BY match_score IS NULL, match_score DESC, first_seen_at DESC"
    )
    # Publish dates carry mixed UTC offsets, so the age cutoff is applied in
    # Python rather than SQL, and any limit after it.
    if limit and max_age_days is None:
        sql += f" LIMIT {int(limit)}"

    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    if max_age_days is not None:
        rows = [r for r in rows if published_within(r["published_at"], max_age_days)]
    return rows[:limit] if limit else rows


def unscored_jobs(limit: int | None = None) -> list[dict]:
    """Rows that have never been through the scorer."""
    sql = (
        "SELECT * FROM sourced_jobs WHERE match_score IS NULL "
        "AND review_status = 'new' ORDER BY first_seen_at DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql)]


def all_companies() -> list[str]:
    with _connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT company FROM sourced_jobs ORDER BY company"
        )]


def counts_by_status() -> dict[str, int]:
    with _connect() as conn:
        return {
            r["review_status"]: r["n"]
            for r in conn.execute(
                "SELECT review_status, COUNT(*) AS n FROM sourced_jobs GROUP BY review_status"
            )
        }
