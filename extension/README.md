# Should I Apply? — Chrome extension

Scores the job posting you are currently looking at against `resume/master.yaml`
and gives you a verdict: APPLY, MAYBE, SKIP, or NO.

It can also fill in the application form. It submits on its own only when every
question has a known answer and no CAPTCHA guards the form; otherwise it fills
what it can and you submit.

## How it fits together

The extension reads the posting off the page and sends it to a small server
running on your own machine. That server holds the Anthropic credentials, your
autofill profile, and your resume file, and calls the scoring code in `tools/`.
The browser never sees an API key, which matters because anyone who installs an
extension can read its source.

```
page  ->  content script  ->  service worker  ->  127.0.0.1:8765  ->  tools/scorer.py
```

## Setup

Start the server from a shell where `ANTHROPIC_API_KEY` is set:

```bash
cd ~/resume-agent && python3 server/score_server.py
```

For autofill, copy `config/profile.example.yaml` to `config/profile.yaml` and
fill it in, and save your resume PDF as `resume/master_resume.pdf`. Both are
gitignored.

Then load the extension:

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked** and choose the `extension/` folder

The toolbar popup shows whether it can reach the server.

## Using it

On a page that looks like a job posting, the panel appears on its own and
scores it. On any other page, or if you turn off auto-scoring, open the popup
and click **Score this page**.

Strengths and Gaps are collapsible, each showing a count, so the verdict stays
the first thing you read.

The verdict combines two things. The fit score comes from the same scorer the
rest of the repo uses, on the same thresholds as `app.py`:

| Verdict | Meaning |
|---|---|
| APPLY | 70 or above |
| MAYBE | 50 to 69 |
| SKIP | below 50 |
| NO | the posting rules out visa sponsorship, whatever the score |

A sponsorship exclusion outranks the score, because a strong match at a company
that will not sponsor is still not applicable. Two checks run: a phrase scan of
the description, and the model's own reading. Either one is enough to block, and
the panel quotes the sentence that triggered it so you can judge for yourself.

## Actions

On an APPLY or MAYBE the panel offers three things:

- **Generate tailored resume** runs the existing tailor and LaTeX pipeline and
  writes a PDF into `applications/`.
- **Draft cold message** writes a short outreach note grounded in
  `resume/master.yaml`, with a subject line and a `[name]` placeholder. It lands
  in an editable box with a copy button, because a draft is a starting point.
- **Autofill application** fills the form on the page from
  `config/profile.yaml` and attaches `resume/master_resume.pdf`.

On a SKIP none is offered, since spending effort on a posting you were just told
to skip is the wrong default. A quiet "Draft anyway" link is there when you
disagree. On a NO, where the posting rules out sponsorship, nothing is offered
at all.

The message prompt carries `master.yaml`'s own `agent_rules` forward, so it
cannot claim experience the resume does not contain, and it inherits the
no-em-dash rule the rest of the repo follows.

## Autofill rules

Autofill runs only when you click the button, never on page load. It outlines
what it filled in green and what it left for you in amber, lists the questions
that need you in the panel, and never overwrites a field you already typed in.

It never:

- writes an answer to a free-response question
- guesses at a question it does not recognize
- ticks an arbitration, consent, or policy agreement
- gets past a CAPTCHA

It submits only when none of those are on the form and it can find the submit
button. Even then it counts down five seconds with a cancel button, and closing
the panel cancels. It checks for a CAPTCHA again at the moment of submitting,
and it will not submit twice on the same page.

Work authorization questions are matched on their exact phrasing. "Will you
require sponsorship?" and "Are you authorized to work without sponsorship?" get
opposite answers from the same profile. Anything phrased otherwise is left for
you. "Authorized to work in the country where the job is located" is answered
only when the posting's own structured data says the job is in the US.

Self-identification questions are answered only for the values you set in
`profile.yaml`. Blank ones are left alone.

## Cost

One scoring call per posting you open, and results are cached per URL, so
revisiting a posting is free. The resume and the cold message each cost an
extra call, and only when you click them. Autofill makes no model calls.

## Notes

- The content script matches all URLs so it works on any careers page. It only
  does anything when the page looks like a job posting.
- The server binds to `127.0.0.1`, so nothing outside your machine can reach it.
  It also refuses requests from websites, checking both the `Origin` and `Host`
  headers, because the scripts on pages you visit run on your machine too.
- Company name is read from the page, its title, or the board URL. It falls back
  to asking a model only when none of those work, since that costs an extra call.
