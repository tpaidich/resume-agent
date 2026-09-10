# Should I Apply? — Chrome extension

Scores the job posting you are currently looking at against `resume/master.yaml`
and gives you a verdict: APPLY, MAYBE, SKIP, or NO.

Nothing here applies to a job. The panel can generate a tailored resume PDF into
`applications/`; sending it is always your step.

## How it fits together

The extension reads the posting off the page and sends it to a small server
running on your own machine. That server holds the Anthropic credentials and
calls the scoring code in `tools/`. The browser never sees an API key, which
matters because anyone who installs an extension can read its source.

```
page  ->  content script  ->  service worker  ->  127.0.0.1:8765  ->  tools/scorer.py
```

## Setup

Start the server from a shell where `ANTHROPIC_API_KEY` is set:

```bash
cd ~/resume-agent && python3 server/score_server.py
```

Then load the extension:

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked** and choose the `extension/` folder

The toolbar popup shows whether it can reach the server.

## Using it

On a page that looks like a job posting, the panel appears on its own and
scores it. On any other page, or if you turn off auto-scoring, open the popup
and click **Score this page**.

Strengths and Gaps are collapsible. Strengths start closed and Gaps start
closed too, each showing a count, so the verdict stays the first thing you read.

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

On an APPLY or MAYBE the panel offers two things, and neither one sends anything:

- **Generate tailored resume** runs the existing tailor and LaTeX pipeline and
  writes a PDF into `applications/`.
- **Draft cold message** writes a short outreach note grounded in
  `resume/master.yaml`, with a subject line and a `[Name]` placeholder. It lands
  in an editable box with a copy button, because a draft is a starting point.

On a SKIP neither is offered, since spending two model calls on a posting you
were just told to skip is the wrong default. A quiet "Draft anyway" link is
there when you disagree. On a NO, where the posting rules out sponsorship,
nothing is offered at all.

The message prompt carries `master.yaml`'s own `agent_rules` forward, so it
cannot claim experience the resume does not contain, and it inherits the
no-em-dash rule the rest of the repo follows.

## Cost

One scoring call per posting you open, and results are cached per URL, so
revisiting a posting is free. The resume and the cold message each cost an
extra call, and only when you click them.

## Notes

- The content script matches all URLs so it works on any careers page. It only
  does anything when the page looks like a job posting.
- The server binds to `127.0.0.1`, so nothing outside your machine can reach it.
- Company name is read from the page, its title, or the board URL. It falls back
  to asking a model only when none of those work, since that costs an extra call.
