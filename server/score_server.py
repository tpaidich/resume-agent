"""Local scoring server for the Chrome extension.

The extension's content script reads the job posting off the page and posts it
here. This process holds the Anthropic credentials and calls the scoring code
that already lives in tools/; the browser never sees an API key.

Standard library only, deliberately. This machine's Python environment is
shared with other projects, so the server adds no dependencies of its own.

It binds to 127.0.0.1, so nothing outside this machine can reach it.

Run it from the repo root:
    python server/score_server.py

Nothing here applies to a job. /score returns a verdict; /resume writes a
tailored PDF into applications/. Submitting an application stays manual.
"""
import json
import os
import re
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# tools/latex_compiler.py and tools/tailor.py resolve resume/ and applications/
# relative to the working directory.
os.chdir(_ROOT)

import yaml

HOST = "127.0.0.1"
PORT = int(os.environ.get("SCORE_SERVER_PORT", "8765"))

# Thresholds match the ones app.py already uses, so the extension and the
# Streamlit app never disagree about the same posting.
MUST_APPLY = 70
MIN_SCORE = 50

_master_lock = threading.Lock()
_master = None

# Generating a resume runs two Claude calls and a LaTeX compile. Serialize it
# so a double-click cannot start two compiles into the same output path.
_resume_lock = threading.Lock()


def _anthropic_client():
    """Reuse the client tools/scorer.py already constructed.

    Building a second one would work, but this keeps every call in the process
    going through the same configured client.
    """
    from tools.scorer import client
    return client


def _load_master():
    global _master
    with _master_lock:
        if _master is None:
            with open(os.path.join(_ROOT, "resume", "master.yaml")) as f:
                _master = yaml.safe_load(f)
        return _master


def verdict_for(score: int, blocked: bool) -> tuple[str, str]:
    """Map a 0-100 score plus the sponsorship check onto a verdict.

    A sponsorship exclusion outranks the score: a 90-point match at a company
    that will not sponsor is still not applicable.
    """
    if blocked:
        return "NO", "This posting rules out visa sponsorship"
    if score >= MUST_APPLY:
        return "APPLY", "Strong match"
    if score >= MIN_SCORE:
        return "MAYBE", "Partial match, worth a read"
    return "SKIP", "Weak match"


def score_posting(payload: dict) -> dict:
    from tools.scorer import score_fit
    from tools.scraper import _resolve_company
    from tools.sourcing.sponsorship_filter import scan_description

    description = (payload.get("text") or "").strip()
    title = (payload.get("title") or "").strip() or "Unknown Role"
    url = (payload.get("url") or "").strip()
    company = (payload.get("company") or "").strip()

    if len(description) < 200:
        return {
            "ok": False,
            "error": "Could not find enough job description text on this page.",
        }

    # The page often names the company outright. Fall back to the existing
    # resolver only when it does not, since that can cost an extra call.
    if not company or company.lower() in ("unknown", ""):
        company = _resolve_company(None, description, title)

    job = {"title": title, "company": company, "url": url, "description": description}
    result = score_fit(job, _load_master())

    # Two independent sponsorship checks. The regex scan is free and precise;
    # the model's judgement catches phrasings the patterns miss. Either one
    # firing is enough to block, and the response says which did.
    regex_hit, evidence = scan_description(description)
    model_hit = bool(result.get("excludes_sponsorship"))
    blocked = regex_hit or model_hit

    score = int(result.get("score", 0))
    label, headline = verdict_for(score, blocked)

    return {
        "ok": True,
        "verdict": label,
        "headline": headline,
        "score": score,
        "title": title,
        "company": company,
        "url": url,
        "sponsorship_blocked": blocked,
        "sponsorship_evidence": evidence if regex_hit else None,
        "sponsorship_source": (
            "job description text" if regex_hit else ("model read" if model_hit else None)
        ),
        "strengths": result.get("strengths", [])[:5],
        "gaps": result.get("gaps", [])[:5],
        "thresholds": {"apply": MUST_APPLY, "maybe": MIN_SCORE},
    }


def generate_resume(payload: dict) -> dict:
    from tools.latex_compiler import compile_pdf
    from tools.tailor import tailor_resume
    from tools.tracker import track_application

    description = (payload.get("text") or "").strip()
    title = (payload.get("title") or "").strip() or "Unknown Role"
    company = (payload.get("company") or "").strip() or "Unknown"
    url = (payload.get("url") or "").strip()
    gaps = payload.get("gaps") or []

    if len(description) < 200:
        return {"ok": False, "error": "Not enough job description text to tailor against."}

    job = {"title": title, "company": company, "url": url, "description": description}

    with _resume_lock:
        placeholders = tailor_resume(job, _load_master(), gaps=gaps)
        pdf_path = compile_pdf(placeholders, job)

    score = {"score": payload.get("score", 0), "strengths": payload.get("strengths", []), "gaps": gaps}
    try:
        track_application(url, job, score, pdf_path=pdf_path, status="ready")
    except Exception as e:
        print(f"[server] tracker write failed (resume still saved): {e}")

    return {"ok": True, "pdf_path": pdf_path, "abs_path": os.path.join(_ROOT, pdf_path)}


# The message structure is fixed here rather than left to the model. Asking a
# model to reproduce a template reliably is a losing game; filling four named
# slots into scaffolding you control is not. Edit the wording here and every
# future draft follows it.
#
# The name and the current-role phrase are read at runtime, not written in, so
# this file carries no personal data and the repo can be public.

OUTREACH_CONFIG = os.path.join(_ROOT, "config", "outreach.yaml")

MESSAGE_TEMPLATE = """Hi [name]!

My name is {first_name} and I'm currently {current_role}. I just applied to the \
{title} role at {company}, and it feels very close to my experiences as \
{relevant_experience}

I wear a lot of hats right now, whether it comes to:
{bullets}
and I was doing this same kind of work before too, {previous_work}

I would really flourish in an environment like {company}. I've attached my \
resume here as well and I would love to move forward.

Thank you,
{first_name}"""


def _first_name(master: dict) -> str:
    """First name for the greeting and the sign-off.

    Uses the formal name, not preferred_name: a cold email to someone who has
    never met you reads better with the name on the resume attached to it.
    """
    meta = master.get("meta", {})
    name = (meta.get("name") or meta.get("preferred_name") or "").strip()
    return name.split()[0] if name else "I"


def _current_role_phrase(master: dict) -> str:
    """How the opener describes what you do now.

    Taken from config/outreach.yaml when it exists, because the phrasing you
    want is rarely the literal job title. Falls back to the most recent role in
    the resume so the tool still works with no config at all.
    """
    try:
        with open(OUTREACH_CONFIG) as f:
            phrase = (yaml.safe_load(f) or {}).get("current_role")
        if phrase:
            return str(phrase).strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[server] could not read {OUTREACH_CONFIG}: {e}")

    for exp in master.get("experience", []):
        role, company = exp.get("role"), exp.get("company")
        if role and company:
            return f"a {role} at {company}"
    return "early in my career"


def draft_message(payload: dict) -> dict:
    """Fill the cold message template for this posting.

    The model supplies four slots, all of which must come from the resume. It
    never writes the surrounding message, so the structure cannot drift.

    This drafts text and returns it. It does not send anything.
    """
    from tools.scorer import _build_resume_text

    description = (payload.get("text") or "").strip()
    title = (payload.get("title") or "").strip() or "the role"
    company = (payload.get("company") or "").strip() or "your team"

    if len(description) < 200:
        return {"ok": False, "error": "Not enough job description text to write against."}

    master = _load_master()
    rules = master.get("agent_rules", [])

    prompt = f"""You are filling four slots in a fixed cold outreach message. You are not
writing the message itself, only the slots. Each one must be grounded in the resume below.

RULES, breaking any of these is a failure:
{chr(10).join(f'- {r}' for r in rules)}
- NEVER use em dashes. Use a comma or semicolon.
- Never state experience, a metric, or a tool that is not in the resume below.
- Write in first person, continuing the candidate's own sentence. Do not start a new one.
- No corporate filler and no adjectives about the company.

RESUME:
{_build_resume_text(master)}

TARGET JOB: {title} at {company}
DESCRIPTION:
{description[:2500]}

Fill these four slots:

1. "relevant_experience" completes this sentence:
   "I just applied to the {title} role at {company}, and it feels very close to my experiences as ..."
   A short phrase naming the roles or kind of work that line up best with this job.
   Lowercase start, no trailing period. Under 20 words.

2. "bullets": exactly 4 short bullets completing "I wear a lot of hats right now, whether it comes to:"
   Each starts with a gerund, for example "running weekly sprint planning across engineering and research".
   Draw them from the CURRENT role first, and pick the four that matter most for this specific job.
   No leading dash, no trailing period. Under 16 words each.

3. "previous_work" completes:
   "and I was doing this same kind of work before too, ..."
   One clause pointing at earlier roles in the resume, naming something concrete.
   Lowercase start, no trailing period. Under 30 words.

4. "subject": a short specific email subject line, under 60 characters.

Return JSON only, no prose, no markdown fences:
{{"relevant_experience": "...", "bullets": ["...", "...", "...", "..."], "previous_work": "...", "subject": "..."}}"""

    msg = _anthropic_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[start:end])
    except Exception:
        return {"ok": False, "error": "Could not parse the drafted message."}

    def clean(value: str) -> str:
        # The no-em-dash rule is in the prompt; this enforces it regardless.
        return str(value or "").replace("\u2014", ", ").replace("\u2013", ", ").strip()

    bullets = [clean(b).lstrip("-\u2022 ").rstrip(".") for b in (data.get("bullets") or [])]
    bullets = [b for b in bullets if b][:4]
    if len(bullets) < 3:
        return {"ok": False, "error": "The draft came back without enough bullets. Try again."}

    body = MESSAGE_TEMPLATE.format(
        first_name=_first_name(master),
        current_role=_current_role_phrase(master),
        title=title,
        company=company,
        relevant_experience=clean(data.get("relevant_experience")).rstrip("."),
        bullets="\n".join(f"- {b}" for b in bullets),
        previous_work=clean(data.get("previous_work")).rstrip("."),
    )

    subject = re.sub(r"\s*,\s*", ", ", clean(data.get("subject")))
    subject = re.sub(r"\s{2,}", " ", subject).strip(" ,")

    return {
        "ok": True,
        "subject": subject[:200],
        "body": body,
        "company": company,
        "title": title,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: dict):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        # The extension's service worker is the only intended caller, and the
        # server is bound to loopback, so a permissive origin is safe here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "resume-agent score server"})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        routes = {
            "/score": score_posting,
            "/resume": generate_resume,
            "/message": draft_message,
        }
        handler = routes.get(self.path)
        if handler is None:
            self._send(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            self._send(400, {"ok": False, "error": f"bad request body: {e}"})
            return

        try:
            self._send(200, handler(payload))
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        print(f"[server] {fmt % args}")


def main():
    # Python block-buffers stdout when it is redirected to a file or a pipe,
    # which hides the startup banner and the request log from anyone running
    # this with output redirected. Line buffering keeps it readable.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    try:
        import anthropic  # noqa: F401
        from tools.scorer import client  # noqa: F401
    except Exception as e:
        print(f"[server] Could not initialize the Anthropic client: {e}")
        print("[server] Start this from a shell where ANTHROPIC_API_KEY is set.")
        sys.exit(1)

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        # Almost always a second copy of this server, which is worth saying
        # plainly rather than raising a socket traceback at someone.
        if e.errno in (48, 98):  # EADDRINUSE on macOS, on Linux
            print(f"[server] Port {PORT} is already in use.")
            print("[server] Another copy of this server is probably running.")
            print(f"[server] Check with:  lsof -nP -iTCP:{PORT} -sTCP:LISTEN")
            print(f"[server] Stop it with: pkill -f score_server.py")
            print(f"[server] Or run on another port: SCORE_SERVER_PORT=8766 python3 server/score_server.py")
            print("[server] If you change the port, update extension/background.js and manifest.json to match.")
            sys.exit(1)
        raise

    print(f"[server] listening on http://{HOST}:{PORT}")
    print("[server] endpoints: GET /health, POST /score, POST /resume, POST /message")
    print("[server] stop with Ctrl-C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
