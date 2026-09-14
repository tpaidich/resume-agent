"""The company registry: every ATS board the pipeline polls.

Lives in applications/sourced_jobs.db next to sourced_jobs. Rows come from
three places, recorded in `source`:

    manual        config/companies.yaml, the hand-curated list
    simplifyjobs  slugs lifted from SimplifyJobs listing links
    yc            the YC directory sweep in discover_companies.py

The pipeline reads this table instead of the yaml file, so discovery grows the
polling pool without anyone editing config. companies.yaml still owns
sponsorship declarations; that is a judgment, not something discovery can find.

Two bookkeeping tables sit beside it so discovery does not repeat itself:
ats_probes remembers which (slug, platform) guesses came back empty, and
yc_sweep remembers when each YC company was last looked at and how it was found.
"""
import json
from datetime import datetime, timedelta, timezone

import yaml

from .job_store import _connect
from .paths import COMPANIES_FILE

PLATFORMS = ("greenhouse", "ashby", "lever")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    slug          TEXT NOT NULL,
    platform      TEXT NOT NULL CHECK(platform IN ('greenhouse','ashby','lever')),
    company_name  TEXT,
    website       TEXT,
    source        TEXT,
    first_seen    TEXT,
    last_checked  TEXT,
    last_had_jobs INTEGER,
    PRIMARY KEY (slug, platform)
);

CREATE TABLE IF NOT EXISTS ats_probes (
    slug          TEXT NOT NULL,
    platform      TEXT NOT NULL,
    last_status   INTEGER,
    had_jobs      INTEGER,
    empty_streak  INTEGER NOT NULL DEFAULT 0,
    last_probed   TEXT NOT NULL,
    PRIMARY KEY (slug, platform)
);

CREATE TABLE IF NOT EXISTS yc_sweep (
    yc_slug        TEXT PRIMARY KEY,
    website        TEXT,
    last_swept     TEXT NOT NULL,
    found_slug     TEXT,
    found_platform TEXT,
    found_via      TEXT
);

CREATE TABLE IF NOT EXISTS discovery_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)


# --- companies -------------------------------------------------------------

def upsert_company(
    slug: str,
    platform: str,
    company_name: str | None,
    source: str,
    website: str | None = None,
    had_jobs: bool | None = None,
) -> bool:
    """Add a board, or fill gaps in one already known. Returns True if new.

    The first source to find a board keeps the credit, and an existing name is
    never overwritten; seed_from_yaml is the one caller allowed to rename.
    """
    slug = slug.strip().lower()
    stamp = now()
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM companies WHERE slug = ? AND platform = ?", (slug, platform)
        ).fetchone()
        conn.execute(
            """INSERT INTO companies
               (slug, platform, company_name, website, source, first_seen,
                last_checked, last_had_jobs)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(slug, platform) DO UPDATE SET
                 company_name  = COALESCE(companies.company_name, excluded.company_name),
                 website       = COALESCE(companies.website, excluded.website),
                 last_checked  = COALESCE(excluded.last_checked, companies.last_checked),
                 last_had_jobs = COALESCE(excluded.last_had_jobs, companies.last_had_jobs)""",
            (
                slug, platform, company_name, website, source, stamp,
                stamp if had_jobs is not None else None,
                None if had_jobs is None else int(had_jobs),
            ),
        )
    return exists is None


def seed_from_yaml(path: str | None = None) -> int:
    """Copy companies.yaml into the table as source='manual'. Returns rows added.

    Curated names win over discovered ones, because the sponsorship registry
    looks postings up by that name.
    """
    with open(path or COMPANIES_FILE) as f:
        entries = yaml.safe_load(f) or []

    added = 0
    stamp = now()
    with _connect() as conn:
        for entry in entries:
            platform = str(entry.get("ats", "")).strip().lower()
            slug = str(entry.get("token") or "").strip().lower()
            name = entry.get("company")
            if platform not in PLATFORMS or not slug or not name:
                print(f"[companies] skipping malformed companies.yaml entry: {entry}")
                continue
            exists = conn.execute(
                "SELECT 1 FROM companies WHERE slug = ? AND platform = ?", (slug, platform)
            ).fetchone()
            conn.execute(
                """INSERT INTO companies (slug, platform, company_name, source, first_seen)
                   VALUES (?,?,?,'manual',?)
                   ON CONFLICT(slug, platform) DO UPDATE SET
                     company_name = excluded.company_name,
                     source = 'manual'""",
                (slug, platform, name, stamp),
            )
            added += exists is None
    return added


def pollable_companies(dead_recheck_days: float) -> list[dict]:
    """Boards worth polling this pass.

    A board whose last poll came back empty is only retried once it has gone
    dead_recheck_days without a check, so thousands of discovered boards that
    are not hiring do not slow every pass.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT slug, platform, company_name AS name FROM companies
               WHERE last_had_jobs IS NOT 0
                  OR last_checked IS NULL
                  OR last_checked < ?
               ORDER BY last_had_jobs IS NULL, last_had_jobs DESC, slug""",
            (_days_ago(dead_recheck_days),),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_checked(results: list[tuple[str, str, bool]]):
    """Record (slug, platform, had_jobs) for boards the pipeline just polled."""
    stamp = now()
    with _connect() as conn:
        conn.executemany(
            "UPDATE companies SET last_checked = ?, last_had_jobs = ? "
            "WHERE slug = ? AND platform = ?",
            [(stamp, int(had), slug, platform) for slug, platform, had in results],
        )


def known_boards() -> set[tuple[str, str]]:
    with _connect() as conn:
        return {(r[0], r[1]) for r in conn.execute("SELECT slug, platform FROM companies")}


# --- probe cache -------------------------------------------------------------

def probe_is_cached_empty(slug: str, platform: str, streak: int, retry_days: float) -> bool:
    """True if this guess came back empty on `streak` consecutive probes recently."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT empty_streak, last_probed FROM ats_probes WHERE slug = ? AND platform = ?",
            (slug, platform),
        ).fetchone()
    return bool(row and row["empty_streak"] >= streak and row["last_probed"] >= _days_ago(retry_days))


def record_probe(slug: str, platform: str, status: int, had_jobs: bool):
    """Store a probe result. Network failures (status 0) leave the streak alone."""
    empty = status != 0 and not had_jobs
    with _connect() as conn:
        conn.execute(
            """INSERT INTO ats_probes (slug, platform, last_status, had_jobs, empty_streak, last_probed)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(slug, platform) DO UPDATE SET
                 last_status  = excluded.last_status,
                 had_jobs     = excluded.had_jobs,
                 empty_streak = CASE WHEN ? = 0 THEN ats_probes.empty_streak
                                     WHEN ? THEN ats_probes.empty_streak + 1
                                     ELSE 0 END,
                 last_probed  = excluded.last_probed""",
            (slug, platform, status, int(had_jobs), int(empty), now(), status, int(empty)),
        )


# --- YC sweep state ------------------------------------------------------------

def yc_swept_since(days: float) -> dict[str, str | None]:
    """YC slugs swept within `days`, mapped to how they were found (or None)."""
    with _connect() as conn:
        return {
            r["yc_slug"]: r["found_via"]
            for r in conn.execute(
                "SELECT yc_slug, found_via FROM yc_sweep WHERE last_swept >= ?", (_days_ago(days),)
            )
        }


def yc_found_slugs() -> set[str]:
    with _connect() as conn:
        return {r[0] for r in conn.execute("SELECT yc_slug FROM yc_sweep WHERE found_slug IS NOT NULL")}


def record_yc_sweep(yc_slug: str, website: str, found: tuple[str, str, str] | None):
    slug, platform, via = found if found else (None, None, None)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO yc_sweep (yc_slug, website, last_swept, found_slug, found_platform, found_via)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(yc_slug) DO UPDATE SET
                 website = excluded.website, last_swept = excluded.last_swept,
                 found_slug = excluded.found_slug, found_platform = excluded.found_platform,
                 found_via = excluded.found_via""",
            (yc_slug, website, now(), slug, platform, via),
        )


# --- run log ---------------------------------------------------------------------

def start_run(source: str) -> int:
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO discovery_runs (source, started_at) VALUES (?, ?)", (source, now())
        ).lastrowid


def finish_run(run_id: int, stats: dict):
    with _connect() as conn:
        conn.execute(
            "UPDATE discovery_runs SET finished_at = ?, stats = ? WHERE run_id = ?",
            (now(), json.dumps(stats, sort_keys=True), run_id),
        )


def summary() -> dict:
    with _connect() as conn:
        by_source = {
            f"{r['source']}/{r['platform']}": r["n"]
            for r in conn.execute(
                "SELECT source, platform, COUNT(*) AS n FROM companies GROUP BY source, platform"
            )
        }
        liveness = {
            {1: "had_jobs", 0: "empty", None: "never_polled"}[r["last_had_jobs"]]: r["n"]
            for r in conn.execute(
                "SELECT last_had_jobs, COUNT(*) AS n FROM companies GROUP BY last_had_jobs"
            )
        }
        yc = {
            (r["found_via"] or "not_found"): r["n"]
            for r in conn.execute("SELECT found_via, COUNT(*) AS n FROM yc_sweep GROUP BY found_via")
        }
        runs = [
            {"source": r["source"], "started_at": r["started_at"],
             "finished_at": r["finished_at"], "stats": json.loads(r["stats"] or "{}")}
            for r in conn.execute(
                "SELECT * FROM discovery_runs ORDER BY run_id DESC LIMIT 6"
            )
        ]
        total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    return {"total": total, "by_source": by_source, "liveness": liveness,
            "yc_sweep": yc, "recent_runs": runs}
