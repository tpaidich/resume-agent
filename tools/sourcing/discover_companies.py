"""Grow the company registry beyond the hand-curated list.

Two strategies, run separately, both writing to the `companies` table that the
sourcing pipeline polls:

  simplify  Pull the SimplifyJobs listing files and lift the board slug out of
            every Greenhouse, Ashby, or Lever apply link. The slug is in the
            URL, so no ATS request is made. Cheap; run weekly.
  yc        Walk the YC directory. For each company, guess slugs from its
            domain and probe the three ATS APIs, then fall back to grepping its
            careers page for a board link. Expensive; run monthly.

  python -m tools.sourcing.discover_companies simplify
  python -m tools.sourcing.discover_companies yc                # currently hiring
  python -m tools.sourcing.discover_companies yc --all --limit 500
  python -m tools.sourcing.discover_companies stats

Every run is logged to discovery_runs with its counters. For the YC sweep those
include hits from slug guessing versus hits from careers pages, and the requests
each spent, which is what decides whether guessing is worth keeping.

A guessed slug can land on a live board that belongs to a different company
with the same name, and ordinary words (hive, radar, tempo) are where that
happens. A guess that is a dictionary word is kept only when the board
mentions the company's domain. Boards that fail are still added, as source
'yc_unmatched', since they are live boards all the same.

Only public data is read. Requests to any one host are spaced 0.3-0.5s apart,
careers pages are fetched only where robots.txt allows, and a guess that came
back empty on two consecutive probes is not probed again for PROBE_RETRY_DAYS.
"""
import argparse
import html
import json
import random
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from urllib.parse import quote, unquote, urlparse
from urllib.robotparser import RobotFileParser

import requests

from . import company_store

USER_AGENT = "Mozilla/5.0 (compatible; ResumeAgent/1.0)"
TIMEOUT = 15

YC_DIRECTORY = {
    "hiring": "https://yc-oss.github.io/api/companies/hiring.json",
    "all": "https://yc-oss.github.io/api/companies/all.json",
}

# The internship repo is renamed each cycle (Summer2026-Internships no longer
# resolves). Check github.com/SimplifyJobs when a run reports a repo failure.
SIMPLIFY_REPOS = ("New-Grad-Positions", "Summer2027-Internships")
# listings.json keeps every posting the repo has ever carried, inactive ones
# included, so it names about ten times as many boards as the README table.
SIMPLIFY_LISTINGS = "https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/.github/scripts/listings.json"
SIMPLIFY_README = "https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/README.md"

PROBE_URLS = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
}
# The Greenhouse probe leaves out descriptions to stay light; verifying a guess
# needs them.
GREENHOUSE_WITH_CONTENT = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

# In testing on 60 YC companies, every guess that reached another company's
# board was an ordinary word the board never tied to the company's domain:
# hive, radar, and tempo each reached unrelated companies, twice over for hive
# and tempo. Every correct board with a dictionary-word slug (stripe, sift,
# gusto, lob, amplitude) named its domain. Comparing description vocabulary was
# tried first and let two wrong boards through, since long postings match common
# words by chance. The one known false rejection is quartzy, an obscure word;
# its board is still polled as 'yc_unmatched', just without the company name.
WORD_LIST = "/usr/share/dict/words"

YC_RESWEEP_DAYS = 30
PROBE_EMPTY_STREAK = 2
PROBE_RETRY_DAYS = 180
MAX_GUESSES = 3
CAREERS_PATHS = ("/careers", "/jobs", "/")
CAREERS_MAX_BYTES = 1_500_000

# Websites on these hosts say nothing about the company's own domain.
_SHARED_HOSTS = (
    "angel.co", "wellfound.com", "github.io", "github.com", "blogspot.com",
    "medium.com", "notion.site", "linktr.ee", "ycombinator.com", "google.com",
    "apple.com", "facebook.com", "linkedin.com", "twitter.com", "x.com",
)
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}

_ATS_LINK = re.compile(
    r"(?:job-boards|boards)\.greenhouse\.io/"
    r"(?:embed/job_(?:board|app)(?:/js)?\?(?:[^\"'\s<>]*?&(?:amp;)?)?for=)?"
    r"(?P<greenhouse>[A-Za-z0-9_-]+)"
    r"|boards-api\.greenhouse\.io/v1/boards/(?P<greenhouse_api>[A-Za-z0-9_-]+)"
    r"|jobs\.ashbyhq\.com/(?P<ashby>[A-Za-z0-9._%-]+)"
    r"|api\.ashbyhq\.com/posting-api/job-board/(?P<ashby_api>[A-Za-z0-9._%-]+)"
    r"|jobs\.lever\.co/(?P<lever>[A-Za-z0-9._-]+)"
    r"|api\.lever\.co/v0/postings/(?P<lever_api>[A-Za-z0-9._-]+)"
)
_JUNK_SLUGS = {"embed", "v1", "api", "jobs", "job_board", "job_app", "posting-api", "static", "assets"}


class _Stats(Counter):
    """A Counter the sweep's worker threads can share."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def incr(self, key: str, n: int = 1):
        with self._lock:
            self[key] += n


class HostThrottle:
    """Space requests to the same host 0.3-0.5s apart, across all threads.

    Work fans out over many careers-page hosts at once, but the three ATS APIs
    are shared by every worker, and each of them still sees one request at a
    time at the same pace a single-threaded loop would produce.
    """

    def __init__(self, low: float = 0.3, high: float = 0.5):
        self.low, self.high = low, high
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, url: str):
        host = urlparse(url).hostname or ""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + random.uniform(self.low, self.high)
        if slot > now:
            time.sleep(slot - now)


def _get(url: str, throttle: HostThrottle, **kwargs) -> requests.Response | None:
    """GET with one retry on a dropped connection, which Lever does now and then."""
    for attempt in (1, 2):
        throttle.wait(url)
        try:
            return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, **kwargs)
        except requests.ConnectionError:
            if attempt == 2:
                return None
        except requests.RequestException:
            return None
    return None


def extract_boards(text: str) -> list[tuple[str, str]]:
    """Every (slug, platform) named by an ATS link in text, in order, deduplicated."""
    found = []
    for match in _ATS_LINK.finditer(text):
        for group, value in match.groupdict().items():
            if not value:
                continue
            slug = unquote(value).strip(" .").lower()
            if slug and slug not in _JUNK_SLUGS:
                found.append((slug, group.split("_")[0]))
    return list(dict.fromkeys(found))


def _host(website: str | None) -> str:
    if not website:
        return ""
    if "://" not in website:
        website = f"http://{website}"
    return (urlparse(website).hostname or "").lower()


def _domain_label(host: str) -> str:
    """The registrable name in a hostname: app.foo.co.uk -> foo."""
    parts = host.removeprefix("www.").split(".")
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2]


def candidate_slugs(company: dict) -> list[tuple[str, str]]:
    """Guesses at a company's board slug, each tagged with where it came from."""
    guesses = []
    host = _host(company.get("website"))
    if host and not host.endswith(_SHARED_HOSTS):
        label = _domain_label(host)
        guesses += [(label.replace("-", ""), "domain"), (label, "domain_dashed")]
    yc_slug = company.get("slug") or ""
    guesses += [(yc_slug.replace("-", ""), "yc_slug"), (yc_slug, "yc_slug_dashed")]
    guesses.append((re.sub(r"[^a-z0-9]", "", (company.get("name") or "").lower()), "name"))

    seen, out = set(), []
    for slug, kind in guesses:
        if len(slug) >= 2 and slug not in seen:
            seen.add(slug)
            out.append((slug, kind))
    return out[:MAX_GUESSES]


@lru_cache(maxsize=1)
def _dictionary() -> frozenset[str]:
    try:
        with open(WORD_LIST) as f:
            return frozenset(word.strip().lower() for word in f)
    except OSError:
        print(f"[yc] no word list at {WORD_LIST}; guesses that are ordinary words go unchecked")
        return frozenset()


def is_dictionary_word(slug: str) -> bool:
    return slug.replace("-", "") in _dictionary()


def board_matches_company(company: dict, slug: str, board_text: str) -> bool:
    """False when a guessed board looks like another company's.

    A dictionary-word slug has to be vouched for by the board naming the
    company's domain. Coined names rarely collide, so they pass on their own.
    """
    host = _host(company.get("website")).removeprefix("www.")
    text = html.unescape(board_text).lower()
    if host and re.search(rf"(?<![a-z0-9-]){re.escape(host)}(?![a-z0-9-])", text):
        return True
    return not is_dictionary_word(slug)


def probe(slug: str, platform: str, throttle: HostThrottle, stats: _Stats, phase: str) -> str | None:
    """The board's response body if it exists and has a posting, else None. Cached."""
    if company_store.probe_is_cached_empty(slug, platform, PROBE_EMPTY_STREAK, PROBE_RETRY_DAYS):
        stats.incr(f"{phase}_probes_skipped_cached")
        return None

    resp = _get(PROBE_URLS[platform].format(slug=quote(slug, safe="")), throttle)
    stats.incr(f"{phase}_probe_requests")
    status = resp.status_code if resp is not None else 0

    had_jobs = False
    if status == 200:
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        jobs = payload if platform == "lever" else (payload or {}).get("jobs") if isinstance(payload, dict) else None
        had_jobs = isinstance(jobs, list) and len(jobs) > 0

    company_store.record_probe(slug, platform, status, had_jobs)
    return resp.text if had_jobs else None


def _robots(origin: str, throttle: HostThrottle) -> RobotFileParser:
    """robots.txt for an origin, read the way urllib.robotparser reads it.

    401/403 disallow everything and other 4xx allow everything, as in the
    standard library. An unreachable or 5xx robots.txt is treated as disallow,
    which is the conservative reading.
    """
    parser = RobotFileParser()
    resp = _get(f"{origin}/robots.txt", throttle)
    if resp is None or resp.status_code >= 500 or resp.status_code in (401, 403):
        parser.disallow_all = True
    elif resp.status_code >= 400:
        parser.allow_all = True
    else:
        parser.parse(resp.text.splitlines())
    return parser


def _read_capped(resp: requests.Response) -> str:
    chunks, size = [], 0
    for chunk in resp.iter_content(65536):
        chunks.append(chunk)
        size += len(chunk)
        if size >= CAREERS_MAX_BYTES:
            break
    resp.close()
    return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")


def sweep_company(
    company: dict, throttle: HostThrottle, stats: _Stats
) -> tuple[tuple[str, str, str] | None, list[tuple[str, str]]]:
    """Find a live board for one YC company.

    Returns (hit, unmatched). hit is (slug, platform, via) or None; unmatched
    lists live boards a guess reached that appear to belong to someone else.
    """
    guesses = candidate_slugs(company)
    tried, unmatched = set(), []
    for slug, kind in guesses:
        for platform in PROBE_URLS:
            tried.add((slug, platform))
            board = probe(slug, platform, throttle, stats, "guess")
            if board is None:
                continue

            if platform == "greenhouse" and is_dictionary_word(slug):
                resp = _get(GREENHOUSE_WITH_CONTENT.format(slug=quote(slug, safe="")), throttle)
                stats.incr("guess_verify_requests")
                if resp is not None and resp.status_code == 200:
                    board = resp.text

            if not board_matches_company(company, slug, board):
                stats.incr("guess_rejected_other_company")
                unmatched.append((slug, platform))
                continue
            stats.incr(f"guess_hit_{kind}")
            return (slug, platform, "guess"), unmatched

    website = company.get("website") or ""
    host = _host(website)
    if not host or host.endswith(_SHARED_HOSTS):
        stats.incr("careers_skipped_no_usable_website")
        return None, unmatched

    scheme = urlparse(website).scheme if "://" in website else "http"
    origin = f"{scheme}://{host}"
    robots = _robots(origin, throttle)

    for path in CAREERS_PATHS:
        url = f"{origin}{path}"
        if not robots.can_fetch(USER_AGENT, url):
            stats.incr("careers_blocked_by_robots")
            continue

        resp = _get(url, throttle, stream=True)
        stats.incr("careers_page_requests")
        if resp is None:
            continue
        if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", "html"):
            resp.close()
            continue

        # A /careers that redirects straight to the board names it in the URL.
        text = " ".join([*(r.url for r in resp.history), resp.url, _read_capped(resp)])
        boards = [b for b in extract_boards(text) if b not in tried]
        if boards:
            stats.incr("careers_pages_with_ats_link")

        for slug, platform in boards:
            tried.add((slug, platform))
            if probe(slug, platform, throttle, stats, "careers"):
                stats.incr("careers_hit")
                if slug not in {g for g, _ in guesses}:
                    stats.incr("careers_hit_slug_not_guessable")
                return (slug, platform, "careers"), unmatched
    return None, unmatched


def _yc_report(s: dict) -> str:
    guess_hits = sum(v for k, v in s.items() if k.startswith("guess_hit_"))
    careers_hits = s.get("careers_hit", 0)
    guess_requests = s.get("guess_probe_requests", 0) + s.get("guess_verify_requests", 0)
    careers_requests = s.get("careers_page_requests", 0) + s.get("careers_probe_requests", 0)

    def per_hit(requests_made, hits):
        return f"{requests_made / hits:.1f} requests per hit" if hits else "no hits"

    by_kind = ", ".join(
        f"{k.removeprefix('guess_hit_')}={v}" for k, v in sorted(s.items()) if k.startswith("guess_hit_")
    )
    return "\n".join([
        f"[yc] swept {s.get('companies_swept', 0)} companies, found boards for {guess_hits + careers_hits}",
        f"[yc]   slug guessing: {guess_hits} hits from {guess_requests} probes "
        f"({per_hit(guess_requests, guess_hits)}; {s.get('guess_probes_skipped_cached', 0)} skipped as cached empty)"
        + (f" [{by_kind}]" if by_kind else ""),
        f"[yc]   {s.get('guess_rejected_other_company', 0)} guessed boards rejected as another company's",
        f"[yc]   careers pages: {careers_hits} hits from {careers_requests} requests "
        f"({per_hit(careers_requests, careers_hits)}); {s.get('careers_hit_slug_not_guessable', 0)} "
        f"of those slugs no guess produced; {s.get('careers_blocked_by_robots', 0)} pages blocked by robots.txt",
        f"[yc]   {s.get('new_boards', 0)} new boards added, {s.get('errors', 0)} errors",
    ])


def run_yc(include_all: bool = False, limit: int | None = None, force: bool = False, workers: int = 8) -> dict:
    company_store.init_db()
    which = "all" if include_all else "hiring"
    resp = requests.get(YC_DIRECTORY[which], headers={"User-Agent": USER_AGENT}, timeout=120)
    resp.raise_for_status()
    directory = [c for c in resp.json() if c.get("slug")]
    if include_all:
        directory = [c for c in directory if c.get("status") in ("Active", "Public")]

    stats = _Stats()
    stats["directory_size"] = len(directory)

    recent = {} if force else company_store.yc_swept_since(YC_RESWEEP_DAYS)
    found = set() if force else company_store.yc_found_slugs()
    todo = [c for c in directory if c["slug"] not in recent and c["slug"] not in found]
    stats["skipped_swept_recently_or_found"] = len(directory) - len(todo)
    if limit:
        todo = todo[:limit]
    print(f"[yc] {len(directory)} companies in the {which} directory; sweeping {len(todo)}")

    run_id = company_store.start_run("yc")
    throttle = HostThrottle()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(sweep_company, c, throttle, stats): c for c in todo}
        for done, future in enumerate(as_completed(futures), start=1):
            company = futures[future]
            try:
                hit, unmatched = future.result()
            except Exception as e:
                stats.incr("errors")
                print(f"[yc] {company.get('name')}: unexpected error — {e}")
                continue

            stats.incr("companies_swept")
            company_store.record_yc_sweep(company["slug"], company.get("website") or "", hit)
            for slug, platform in unmatched:
                # Not this company's board, but a live one worth polling anyway.
                if company_store.upsert_company(slug, platform, None, "yc_unmatched", had_jobs=True):
                    stats.incr("new_unmatched_boards")
                print(f"[yc] {company.get('name')}: {platform}/{slug} is another company's board")
            if hit:
                slug, platform, via = hit
                new = company_store.upsert_company(
                    slug, platform, company.get("name"), "yc",
                    website=company.get("website"), had_jobs=True,
                )
                stats.incr("new_boards" if new else "already_known_boards")
                print(f"[yc] {done}/{len(todo)} {company.get('name')}: {platform}/{slug} via {via}"
                      + ("" if new else " (already known)"))
            if done % 100 == 0:
                print(_yc_report(stats))
    except KeyboardInterrupt:
        print("[yc] interrupted; swept companies are saved and will be skipped next run")
        stats.incr("interrupted")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        snapshot = dict(stats)
        company_store.finish_run(run_id, snapshot)

    print(_yc_report(snapshot))
    return snapshot


_README_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_README_COMPANY = re.compile(r"<td><strong>(.*?)</strong></td>", re.S)


def _simplify_entries(repo: str) -> tuple[list[tuple[str, str]], str]:
    """(company_name, text containing the apply link) for every posting in a repo."""
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(SIMPLIFY_LISTINGS.format(repo=repo), headers=headers, timeout=120)
        resp.raise_for_status()
        return [(e.get("company_name") or "", e.get("url") or "") for e in resp.json()], "listings.json"
    except (requests.RequestException, ValueError) as e:
        print(f"[simplify] {repo}: listings.json unavailable ({e}); falling back to the README")

    resp = requests.get(SIMPLIFY_README.format(repo=repo), headers=headers, timeout=120)
    resp.raise_for_status()
    entries, company = [], ""
    for row in _README_ROW.findall(resp.text):
        match = _README_COMPANY.search(row)
        if match:
            company = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
        # Rows marked "↳" are more roles at the company in the row above.
        entries.append((company, row))
    return entries, "README.md"


def run_simplify(repos: list[str] | None = None) -> dict:
    company_store.init_db()
    run_id = company_store.start_run("simplifyjobs")
    stats = Counter()
    names: dict[tuple[str, str], Counter] = {}

    for repo in repos or SIMPLIFY_REPOS:
        try:
            entries, origin = _simplify_entries(repo)
        except requests.RequestException as e:
            print(f"[simplify] {repo}: could not fetch — {e}")
            stats["repo_failures"] += 1
            continue
        links = 0
        for company, text in entries:
            for board in extract_boards(text):
                links += 1
                names.setdefault(board, Counter())[company] += 1
        stats["postings_scanned"] += len(entries)
        stats["ats_links"] += links
        print(f"[simplify] {repo} ({origin}): {len(entries)} postings, {links} ATS links")

    for (slug, platform), counter in names.items():
        name = counter.most_common(1)[0][0] or None
        new = company_store.upsert_company(slug, platform, name, "simplifyjobs")
        stats["new_boards" if new else "already_known_boards"] += 1
        stats[f"boards_{platform}"] += 1

    snapshot = dict(stats)
    company_store.finish_run(run_id, snapshot)
    print(
        f"[simplify] {len(names)} distinct boards "
        f"({stats['boards_greenhouse']} greenhouse, {stats['boards_ashby']} ashby, "
        f"{stats['boards_lever']} lever); {stats['new_boards']} new"
    )
    return snapshot


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Discover ATS job boards for the sourcing pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)

    simplify = sub.add_parser("simplify", help="mine SimplifyJobs listings (weekly)")
    simplify.add_argument("--repo", action="append",
                          help=f"SimplifyJobs repo to mine; repeatable (default: {', '.join(SIMPLIFY_REPOS)})")

    yc = sub.add_parser("yc", help="sweep the YC directory (monthly)")
    yc.add_argument("--all", dest="include_all", action="store_true",
                    help="every active YC company, not just those marked hiring")
    yc.add_argument("--limit", type=int, help="sweep at most this many companies")
    yc.add_argument("--force", action="store_true",
                    help=f"re-sweep companies already found or swept in the last {YC_RESWEEP_DAYS} days")
    yc.add_argument("--workers", type=int, default=8)

    sub.add_parser("stats", help="print registry size and recent run counters")

    args = parser.parse_args(argv)
    if args.command == "simplify":
        run_simplify(args.repo)
    elif args.command == "yc":
        run_yc(args.include_all, args.limit, args.force, args.workers)
    else:
        company_store.init_db()
        print(json.dumps(company_store.summary(), indent=2))


if __name__ == "__main__":
    main()
