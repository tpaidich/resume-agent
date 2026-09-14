"""
Batch runner. Two modes.

URL mode — run the agent over a file of job URLs:
  python scheduler.py urls.txt          # run once
  python scheduler.py urls.txt 24       # run every 24 hours

Sourcing mode — poll the company boards in the companies table (seeded from
config/companies.yaml, grown by tools/sourcing/discover_companies.py), filter,
score, and queue postings for review:
  python scheduler.py source            # run once
  python scheduler.py source 6          # run every 6 hours
  python scheduler.py source 6 --limit 200

Sourcing is read-only traffic against public APIs and never applies to
anything. Review the queue with:
  streamlit run dashboard/review_app.py
"""
import sys
import time

from agent import main as run_agent

DEFAULT_SOURCE_INTERVAL_HOURS = 6


def run_batch(urls_file: str, interval_hours: float = 0):
    with open(urls_file) as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    print(f"[scheduler] {len(urls)} URLs loaded")
    if interval_hours > 0:
        print(f"[scheduler] Repeating every {interval_hours}h")

    while True:
        for url in urls:
            try:
                run_agent(url)
            except Exception as e:
                print(f"[scheduler] Error on {url}: {e}")

        if interval_hours <= 0:
            break

        print(f"[scheduler] Sleeping {interval_hours}h...")
        time.sleep(interval_hours * 3600)


def run_sourcing(interval_hours: float = 0, score_limit: int | None = None):
    """Poll every registered company board on a loop.

    A few hours between passes is plenty. These boards double as the companies'
    own careers pages, and there is nothing to gain from polling harder.
    """
    from tools.sourcing.pipeline import run_once

    if interval_hours > 0:
        print(f"[scheduler] Sourcing every {interval_hours}h")

    while True:
        try:
            summary = run_once(score=True, score_limit=score_limit)
            print(
                f"[scheduler] {summary['inserted']} new postings, "
                f"{summary['scored']} scored, {summary['seconds']}s"
            )
        except Exception as e:
            print(f"[scheduler] Sourcing pass failed: {e}")

        if interval_hours <= 0:
            break

        print(f"[scheduler] Sleeping {interval_hours}h...")
        time.sleep(interval_hours * 3600)


def _parse_limit(args: list[str]) -> int | None:
    if "--limit" in args:
        i = args.index("--limit")
        if i + 1 < len(args):
            return int(args[i + 1])
    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    target = sys.argv[1]
    positional = [a for a in sys.argv[2:] if not a.startswith("--")]
    if "--limit" in sys.argv:
        limit_index = sys.argv.index("--limit")
        positional = [
            a for i, a in enumerate(sys.argv[2:], start=2)
            if not a.startswith("--") and i != limit_index + 1
        ]

    if target == "source":
        interval = float(positional[0]) if positional else 0
        run_sourcing(interval, score_limit=_parse_limit(sys.argv))
    else:
        interval = float(positional[0]) if positional else 0
        run_batch(target, interval)
