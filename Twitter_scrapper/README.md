# x-leads — finding video-editing clients on X

Finds people on X (Twitter) who are **asking for video/content help**, and
filters out the freelance editors advertising themselves, the YouTube-growth
guru threads, and everyone just chatting about video.

Built for a video editing business doing outbound: the output is a short list
of posts worth replying to, each with a link, the poster's follower count and
bio, and the reason it was picked.

```
x-leads
```

First run opens a browser window to sign in to X. Every run after that is
headless and takes about a minute.

---

## Install

```bash
cd Twitter-Lead-Scraper
python -m pip install -e .
python -m playwright install chromium
```

Then either `x-leads` or `python -m x_leads`.

---

## What it does

```
x-leads --hours 24              # everything new from the last day
x-leads --include-seen          # also re-show leads an earlier run reported
x-leads --min-verdict warm      # skip the maybes
x-leads -f csv -o leads.csv     # a spreadsheet
x-leads --include-rejected      # everything, with the reason each was cut
x-leads --dry-run               # show the searches, run nothing
```

**Leads you have already been shown are skipped.** Every run answers "what's
new?", so running twice in a row legitimately reports nothing the second time —
that is the feature working, not a fault. `--include-seen` shows them again,
and `--forget-seen` wipes the memory entirely.

Only the leads actually *displayed* are remembered. A `--min-verdict warm` run
does not bury that day's cold leads, so widening the net later still surfaces
them.

Sample output:

```
[HOT    17]  @somebrand  (12,400 followers, 3h ago)
  https://x.com/somebrand/status/2087...
    Looking for a full-time video editor who excels at motion graphics.
    Pay: 1.5K SGD / month. What you'll edit: clipping long-form + talking head shorts
    bio: We help founders build their personal brand
    why: demand:looking-for, demand:job-spec, context:budget, context:commitment
```

`why:` is the list of rules that fired. If a lead looks wrong, that line says
which rule to go and fix.

### Verdicts

| | meaning |
|---|---|
| **hot** | an explicit ask with a budget, a job spec, or a request for portfolios |
| **warm** | a clear ask, less detail attached |
| **cold** | probably an ask, thin on context — worth a skim |
| rejected | seller, noise, or off-topic (hidden unless `--include-rejected`) |

---

## Signing in

The session lives in `~/.x_lead_scraper/session.json`. Nothing else is stored,
and no password is ever asked for or saved — the sign-in happens in a real
browser window and only the resulting cookies are kept.

```bash
x-leads --login             # sign in (or switch accounts)
x-leads --session-status    # is the saved session still good?
x-leads --logout            # delete it
x-leads --no-login          # never open a window; fail instead (for cron)
```

When the session expires, a normal run notices and opens the sign-in window by
itself. On a schedule, pass `--no-login` so it exits with a clear error instead
of waiting at an invisible window forever.

> `session.json` holds live tokens. It is as good as a password — keep it off
> shared drives, and run `--logout` on any machine you don't control.

---

## Why it's fast now

The previous scraper ran 20 broad keywords across two result modes — 40
searches, 40 browser launches, scrolling to a fixed depth every time — and
returned a few thousand posts of which almost none were leads.

Four changes, each measured against live X on 2026-08-13:

**Intent phrases, not keywords.** Searching `"youtube"` finds people talking
about YouTube. Searching `"hiring a video editor"` finds buyers. 47 phrases
replace 20 keywords and are far more precise.

**Phrases packed into batches.** X caps a search query at ~512 characters, so
the 47 phrases pack into **4 queries** rather than 47. (446 chars returns
results; 509 returns an empty timeline with no error — the limit is enforced in
code and asserted in the tests, because tripping it looks exactly like "no
leads today".)

**Latest only.** `top` is engagement-ranked, so it surfaces viral commentary
about editing rather than the small, zero-like post from someone who needs an
editor — and it overlaps heavily with `live`. Running both roughly doubled the
time for almost no new leads.

**Stop at the age cutoff, not at a scroll count.** Latest is strictly
reverse-chronological, so the moment a response contains a post older than
`--hours` there is provably nothing newer below and the query is finished. That
is what stops it pulling thousands of stale posts.

It also reads X's own API responses instead of scraping the DOM, which is
cheaper and carries more: full text of long posts (the DOM truncates them
behind "Show more" — and long posts are the ones with the budget in them),
follower counts, bios, and X's self-declared profession field.

---

## Why the results are relevant now

The hard part is not finding posts about video editing. It is that **a keyword
match tells you nothing about which side of the deal the poster is on.** These
two share a phrase, word for word:

```
"Looking for a video editor! $500 per 30-second reel."     <- a client
"Looking for a Video Editor? DM Me!"                       <- a competitor
```

In the old scraper's output, freelance editors advertising themselves
outnumbered actual buyers about three to one — they use the buyer's exact
vocabulary because they are writing bait for it. So the classifier keys on
grammatical stance rather than vocabulary:

- **asking** — "I need", "we're hiring", "drop your portfolio below"
- **offering** — "I'm an editor", "hire me", "are you looking for a…?"

A question aimed at *the reader* is advertising copy, and that single rule
removed the largest category of false positives. It has one exception: a real
job post sometimes opens with a hook ("Need an editor? We're hiring — drop your
reel"), so a post that also instructs applicants survives it.

Run against the old scraper's 167 results, this keeps 21 and rejects 146. Every
rejection that still carries a demand signal was checked by hand and is a
genuine seller pitch.

---

## Tuning it

**The phrase list** is `x_leads/niches/video_editing.json` — edit it, then
`--dry-run` to check the query count. Only add phrases a *buyer* would write;
bare keywords like "video editing" are what made the old version noisy.

**The rules** are `x_leads/leads/patterns.py` (what matches) and
`classifier.py` (what it's worth). After any change:

```bash
python -m pytest tests -q
```

The classifier tests are built from real posts, each one a case that broke an
earlier version of the rules. If a change breaks one, it broke something that
was deliberately fixed.

Getting too much? `--min-verdict warm`, or a shorter `--hours`.
Too little? `--hours 72`, `--min-verdict cold`, or add phrases.
Suspicious? `--include-rejected` shows what was cut and why.

---

## Rate limits

X rate-limits search hard, and a blocked search returns an **empty timeline
rather than an error** — indistinguishable from a quiet one. A blocked query is
therefore retried with a backoff, and if it still fails the run says
`BLOCKED` loudly instead of quietly reporting fewer leads.

If it happens often: drop `--concurrency` to 1, run less frequently, or shorten
`--hours`. Raising `--concurrency` is the quickest way to get limited.

---

## Running it daily

```cmd
run-daily.cmd
```

Writes a dated CSV of new leads only. Nothing schedules it by itself — register
it with Task Scheduler:

```cmd
schtasks /create /tn "X Leads Daily" /sc daily /st 09:00 ^
    /tr "C:\path\to\Twitter-Lead-Scraper\run-daily.cmd"
```

The time of day barely matters: it looks back 24 hours either way.

It passes `--no-login`, so it fails loudly with exit code 3 when the session
expires rather than hanging on a window nobody can see — run `x-leads --login`
when that happens.

A quiet day writes a header-only CSV and exits 0. That is deliberately
distinguishable from a failure, because both failure modes exit non-zero (3 for
an expired session, 4 for a rate limit).

Exit codes: `0` ok, `2` browser missing, `3` not signed in, `4` blocked by X.

---

## Layout

```
x_leads/
├── cli.py                  argparse entry point
├── scraper.py              XLeadScraper facade: niche in, scored leads out
├── models.py               Author, Tweet, Lead
├── state.py                cross-run dedupe for --only-new
├── output.py               digest / json / jsonl / csv
├── auth/session.py         saved session, verification, headful login
├── search/
│   ├── query.py            phrase -> query packing, the 512-char limit
│   ├── parser.py           X GraphQL payload -> Tweet
│   └── collector.py        browser driving, response capture, stop rules
├── leads/
│   ├── patterns.py         the ask / the offer / noise regexes
│   └── classifier.py       weights, thresholds, hard rules
└── niches/video_editing.json
```

Use it as a library:

```python
from x_leads import XLeadScraper, ScrapeConfig

result = await XLeadScraper(ScrapeConfig(hours=24, min_verdict="warm")).run()
for lead in result.leads:
    print(lead.verdict, lead.tweet.url, lead.signals)
```

---

## Notes

- Automating a signed-in X session is against X's terms of service. The risk
  sits with the account, which is why concurrency is low by default and there
  is no aggressive retry loop. Use an account you can afford to lose.
- The classifier's patterns are English-only; `language` in the niche file
  controls what X returns.
- Only tweet IDs are stored between runs (`~/.x_lead_scraper/seen.json`), never
  the posts themselves.
