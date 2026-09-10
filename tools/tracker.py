import json
import os
from datetime import datetime

TRACKER_FILE = "applications/tracker.json"


def track_application(url: str, job: dict, score: dict, pdf_path: str = None, status: str = "ready"):
    os.makedirs("applications", exist_ok=True)

    if os.path.exists(TRACKER_FILE):
        with open(TRACKER_FILE) as f:
            tracker = json.load(f)
    else:
        tracker = []

    entry = {
        "timestamp": datetime.now().isoformat(),
        "url": url,
        "title": job.get("title"),
        "company": job.get("company"),
        "score": score.get("score"),
        "status": status,
        "pdf": pdf_path,
        "strengths": score.get("strengths", []),
        "gaps": score.get("gaps", []),
    }

    for i, existing in enumerate(tracker):
        if existing["url"] == url:
            tracker[i] = entry
            break
    else:
        tracker.append(entry)

    with open(TRACKER_FILE, "w") as f:
        json.dump(tracker, f, indent=2)

    print(f"[tracker] {job.get('company')} — {status} (score: {score.get('score')})")
