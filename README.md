# resume-agent

Tools for running a job search: score a posting against your resume, tailor a
one-page PDF for it, and draft outreach. Nothing here applies to a job. Every
path ends with a file or a draft in front of you.

## What's in here

**Chrome extension plus a local server.** The main thing. Open a job posting and
a panel scores it against your resume, tells you APPLY, MAYBE, SKIP, or NO, and
offers a tailored resume and a cold message. The server runs on your machine and
holds the API credentials, so the browser never sees a key. See
[`extension/README.md`](extension/README.md).

**A CLI agent.** `python3 agent.py <job_url>` scrapes a posting, scores it,
and writes a tailored PDF if it clears the threshold.

**A Streamlit app.** `streamlit run app.py` does the same thing with a UI, plus
a batch search across job boards.

**A sourcing pipeline.** `python3 scheduler.py source 6` polls the company
boards in `config/companies.yaml` every six hours, filters and scores what it
finds, and queues it for review in `streamlit run dashboard/review_app.py`.
Read-only traffic against public ATS APIs.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

Then fill in your own resume. Three files are gitignored because they hold
personal data; each has an example next to it:

| Copy this | To this | Holds |
|---|---|---|
| `resume/master.example.yaml` | `resume/master.yaml` | Your work history, the source of truth |
| `resume/template.example.tex` | `resume/template.tex` | The LaTeX layout and your contact details |
| `config/outreach.example.yaml` | `config/outreach.yaml` | How outreach describes what you do now |

PDF output needs `pdflatex` on your PATH.

## How the scoring works

A posting is scored 0 to 100 against your resume, calibrated so that a large
experience shortfall outweighs a strong skills match. Separately, the job text
is scanned for language that rules out visa sponsorship. That check outranks the
score: a strong match at a company that will not sponsor is still not
applicable, and the panel quotes the sentence that triggered it.

The sponsorship scan matches specific disclaimer phrases rather than the word
"sponsor", which in real postings usually means an executive sponsor, an event
sponsorship, or relocation. Across 1,100 live postings it flagged 2.4% with no
false positives in a hand-checked sample.

## Constraints the writing tools work under

`agent_rules` in `master.yaml` are carried into every prompt that writes on your
behalf. They forbid inventing metrics, adding tools you have not used, and
rewriting your bullets rather than reordering them. The cold message is built
from a fixed template in `server/score_server.py` with the model filling four
named slots, so its structure cannot drift.

## Note on `tools/tailor.py`

The bullet limits and section keys there are named after specific employers.
Adapting this to your own resume means editing that map.
