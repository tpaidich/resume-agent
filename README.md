# resume-agent

Open a job posting. Get told whether to apply.

A Chrome extension reads the posting you're looking at, scores it against your
resume, and gives you a verdict: **APPLY**, **MAYBE**, **SKIP**, or **NO**. If
it's worth pursuing, one click writes a tailored one-page resume PDF and one
click drafts a cold message.

It never applies to anything. It hands you a file and a draft. You send them.

<img width="387" height="706" alt="Screenshot 2026-09-09 at 11 11 24 PM" src="https://github.com/user-attachments/assets/3c44ab3a-69a6-4600-bb54-83b8eff02be9" />

---

## What you need

Four things:

| | |
|---|---|
| **Python 3.10+** | Runs the scoring server |
| **Google Chrome** | Runs the extension |
| **An Anthropic API key** | Does the scoring and writing. Get one at [console.anthropic.com](https://console.anthropic.com) |
| **Your resume, as YAML** | The thing everything is scored against |

Optional: **pdflatex** if you want the tailored resume PDFs. Everything else
works without it. On a Mac, `brew install --cask mactex-no-gui`.

---

## Setup

**1. Clone and install**

```bash
git clone https://github.com/tpaidich/resume-agent.git
cd resume-agent
pip install -r requirements.txt
```

**2. Add your API key**

```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here
```

Put that line in your `~/.zshrc` so it sticks.

**3. Fill in your resume**

Three files hold personal data, so they're not in this repo. Copy each example
and edit it:

```bash
cp resume/master.example.yaml resume/master.yaml
cp resume/template.example.tex resume/template.tex
cp config/outreach.example.yaml config/outreach.yaml
```

- `master.yaml` is your work history. Everything reads from it.
- `template.tex` is the resume layout and your contact details.
- `outreach.yaml` is one line describing what you do now, used in cold messages.

**4. Start the server**

```bash
python3 server/score_server.py
```

Leave it running. It holds your API key so the browser never sees it.

**5. Load the extension**

1. Go to `chrome://extensions`
2. Turn on **Developer mode**, top right
3. Click **Load unpacked** and pick the `extension` folder

Click the toolbar icon. If it says "Server running", you're done.

---

## Using it

Open a job posting. The panel appears and scores it.

| Verdict | Means |
|---|---|
| **APPLY** | 70+. Strong match. |
| **MAYBE** | 50 to 69. Worth a read. |
| **SKIP** | Under 50. Weak match. |
| **NO** | The posting says it won't sponsor a visa. |

**NO overrides the score.** A 90-point match at a company that won't sponsor is
still not applicable, so it says so and shows you the exact sentence.

On APPLY and MAYBE you get two buttons:

- **Generate tailored resume** writes a PDF into `applications/`
- **Draft cold message** writes an outreach note you can edit and copy

On SKIP neither appears, since spending money on a posting you were told to skip
is the wrong default. There's a small "Draft anyway" link if you disagree.

**Cost:** about a tenth of a cent per posting scored. Results are cached per
URL, so reopening a posting is free.

---

## Also in here

Three other ways to use the same engine. All optional.

**Score one job from the terminal**

```bash
python3 agent.py https://job-boards.greenhouse.io/company/jobs/12345
```

**A web app**

```bash
streamlit run app.py
```

**Watch company job boards automatically**

Add companies to `config/companies.yaml`, then:

```bash
python3 scheduler.py source 6
```

Checks every 6 hours for new postings, scores them, and queues them up. Review
what it found with `streamlit run dashboard/review_app.py`.

---

## How it decides

**The fit score** compares the posting to your resume, weighted so that a big
experience gap outweighs a good skills match. A role wanting 8 years when you
have 2 scores low even if you know every tool listed.

**The sponsorship check** is separate and stricter. It looks for phrases like
"unable to sponsor" rather than the word "sponsor", because in real job
descriptions "sponsor" almost always means an executive sponsor, an event
sponsorship, or relocation. Tested across 1,100 live postings: it flagged 2.4%,
with no false positives in a hand-checked sample.

**Guardrails on the writing.** `agent_rules` in your `master.yaml` go into every
prompt that writes for you. They block inventing metrics, adding tools you
haven't used, and rewriting your bullets instead of reordering them. The cold
message uses a fixed template with only four slots filled in, so its structure
can't drift.

---

## Built with

**[Claude API](https://docs.anthropic.com/)** by Anthropic. `claude-haiku-4-5`
scores postings, `claude-sonnet-4-6` writes resumes and outreach.

**Job board APIs**, all public and free, no auth:
[Greenhouse](https://developers.greenhouse.io/job-board.html),
[Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api),
[Remotive](https://remotive.com/api/remote-jobs). Optionally
[JSearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) via RapidAPI
if you set `RAPIDAPI_KEY`.

**[Chrome Extensions, Manifest V3](https://developer.chrome.com/docs/extensions/)**.
The panel renders in a Shadow DOM so job board CSS can't break it.

**Python:** [anthropic](https://github.com/anthropics/anthropic-sdk-python),
[requests](https://requests.readthedocs.io/),
[BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/),
[PyYAML](https://pyyaml.org/), [Streamlit](https://streamlit.io/),
[pandas](https://pandas.pydata.org/), and `sqlite3` from the standard library.

**LaTeX** via pdflatex, using a widely circulated `resume.cls` class (2010,
v0.9) that was not written for this project.

Built with [Claude Code](https://claude.com/claude-code).

---

## Adapting it to you

One rough edge: `tools/tailor.py` has bullet limits and section keys named after
specific employers. Using this with your own resume means editing that map.
