# Upwork Scraper (standalone)

## What This Is

The scraping core of `../Upwork-Job-Scraper`, extracted to run on its own. Same
technique — curl_cffi with `impersonate="chrome"` for the visitor token and the
GraphQL calls — with the database, migrations, and compose stack removed.

**The original project is a separate, untouched copy.** Changes here never affect
it, and it does not import anything from here. Fixes worth sharing must be ported
by hand in either direction.

## Architecture

```
upwork_scraper/
├── config.py                   # Optional env vars — importing never raises
├── errors.py                   # TokenExpired, TokenFetchFailed
├── log_config.py               # init_logger() — all logs to stderr
├── scraper.py                  # UpworkScraper facade: scrape(), scrape_loop()
├── country_filter.py           # CountryFilter — excluded-country rules, no deps
├── cli.py                      # argparse CLI, json/jsonl/csv rendering
├── niches/
│   ├── __init__.py             # Niche dataclass, load_niche(), relevance check
│   └── video.json              # video/YouTube/creator preset (editable)
├── enrich/                     # SEPARATE stage — never touches job scraping
│   ├── client_fetcher.py       # ClientEnricher, parse_client_info()
│   ├── browser_fetcher.py      # BrowserFetcher (patchright + real Chrome)
│   └── throttle.py             # HumanDelay, CircuitBreaker
├── auth/token_manager.py       # visitor_gql_token fetch + 25 min cache
├── models/
│   ├── job_models.py           # Job, JobList — Pydantic GraphQL parsing
│   └── proxy_models.py         # ProxyConfig
├── proxies/proxy_manager.py    # ProxyManager / WebshareProxyManager / NoProxyManager
└── scrapers/job_fetcher.py     # GraphQL query, fetch_jobs_page(), fetch_all_jobs()
```

## Differences From The Original

| | Original | Here |
|---|---|---|
| Storage | PostgreSQL via psycopg3 | none — returns `list[Job]` |
| Package root | `src/` | `upwork_scraper/` |
| Proxies | `WEBSHARE_URL` required | optional, `NoProxyManager` fallback |
| Config import | `os.environ[...]`, raises when unset | `os.getenv(...)`, always safe |
| Entry point | `src.controllers.scraper_controller` | `python -m upwork_scraper` |
| Dedup | `ON CONFLICT (cipher) DO NOTHING` | in-memory per batch / per loop |
| Logs | INFO→stdout | everything→stderr (stdout is for data) |

Additions: niche presets with relevance filtering, country exclusion, backfill of
already-posted jobs, multi-keyword search, real-time feed controls
(`--skip-backlog`, `--max-age`, missed-job warnings), `--pin-proxy`,
JSON/JSONL/CSV output, `UpworkScraper` facade, CLI. User-facing setup lives in
`GUIDE.md`.

## Key Decisions

- **Proxies optional**: The DB was the hard dependency worth removing; Webshare
  was the other one. Without a proxy, concurrency drops from 10 workers to 2
  (`DIRECT_WORKERS`) so a single IP isn't hammered.
- **`highlight` is off whenever a query is set**: Upwork wraps query matches in
  literal `H^ ... ^H` markers when highlighting is on, which corrupts titles and
  descriptions. With no query nothing is highlighted, so `highlight: True` is kept
  there to match the original request exactly.
- **`pin_proxy` is opt-in, not the default**: The visitor token is issued against
  the egress IP that requested it, so rotating proxies mid-run can 401. The
  original rotates per page anyway, so that stayed the default; `--pin-proxy`
  routes every page through the token's proxy instead.
- **Config never raises on import**: The original crashes at import when
  `DATABASE_URL` is unset. A library has to be importable without a `.env`.
- **Logs on stderr**: Lets `python -m upwork_scraper | jq` work.
- **Real-time feed sizing is measured, not guessed**: On 2026-08-12 the live API
  showed the newest job on page 1 aged 10-90s, ~3 new jobs/min site-wide, so one
  50-job page holds ~15 min of backlog. `--interval 60 --pages 1` is therefore
  ~15x oversampled. `scrape_loop` warns when a whole cycle is new, since that is
  the signal the window was actually exceeded.
- **`published_date` is not reliably monotonic**: recency-sorted pages
  occasionally include a job with a much older publish time (2560 min, 5646 min
  observed). Dedup by `cipher` is the trustworthy "is this new" signal;
  `--max-age` is a display filter, not a correctness mechanism.
- **One search per keyword**: Upwork's `userQuery` takes a single string, so N
  keywords cost N requests per cycle. Results merge on `cipher` and
  `matched_query` records every keyword that hit.
- **Page 1 is fetched alone to read `paging.total`**: the original requested
  every offset up to `max_pages` blind. Backfill made that expensive — a keyword
  with 754 results was costing 100 requests instead of 16. The serial first
  request pays for itself as soon as depth exceeds a couple of pages.
- **Relevance terms were tuned against real results, not guessed**: sampling
  `influencer` / `content creator` / `social media video` showed ~1/3 of results
  were marketing roles with no video work. Excludes are checked against the
  title only — an early version also checked skills and wrongly dropped genuine
  video jobs carrying a "Lead Generation" skill tag.
- **UTF-8 forced on stdout/stderr**: job titles are full of arrows, em-dashes and
  non-Latin script. On Windows a *redirected* stream defaults to cp1252 and dies
  with UnicodeEncodeError partway through, so `> jobs.jsonl` silently truncated.
- **Client data IS public; the blocker was Cloudflare, not login**: an early
  conclusion here said "requires a logged-in session", generalising a 403 on the
  job page into an auth wall. Wrong — a browser sees country, city, local time,
  member-since, spend, hires, industry, company size and proposal counts while
  logged out. Only hire-rate %, rating, review count and payment-verified are
  genuinely login-only. GraphQL `jobPubDetails` / `marketplaceJobPosting` /
  `visitorJobDetails` do return oauth2 scope errors, but the page does not.
- **patchright + real Chrome + headful is the only configuration that works**:
  measured against live pages — curl_cffi (7 impersonations), Playwright with
  bundled Chromium or real Chrome, headless or headful, with or without a
  persistent profile, and patchright headless all get a 403 challenge. Stock
  Playwright leaks CDP traces Cloudflare fingerprints; headless is detected
  independently. The window is parked off-screen rather than run headless.
- **Custom UA / stealth args / init scripts HURT patchright**: they help stock
  Playwright and hurt the patched one, whose approach is to look like an
  ordinary Chrome with nothing overridden. Do not "improve" these back in.
- **Do not rotate proxies during enrichment**: a Cloudflare clearance cookie is
  bound to the IP that earned it, so a fresh IP per request guarantees a fresh
  challenge. One pinned proxy plus a persistent profile is what keeps the run
  clean — the opposite of the correct policy for the search API.
- **Parse by page region, and match values by shape**: whole-page searching read
  the site nav's "Proposals that win funding" as the proposal count, and taking
  "the line after the label" returned the tooltip's "Close the tooltip" button.
  Fields are now scoped to the About-the-client / Activity regions and matched
  by their own pattern.
- **Enrichment is a separate *stage*, with merged *output***: it runs after jobs
  are fetched in full, so a failure there can never affect a job scrape — but
  each job carries its poster under `client` in one file, because the point is
  qualifying a job on client history. `--separate-clients` restores the extra
  standalone file. A job never attempted has `client: null`; one attempted and
  failed has a `client` object with `fetch_status` set, keeping "no data" and
  "never tried" distinguishable.
- **Enrichment pacing is the ban-avoidance surface**: one page request per job is
  far heavier than the search API. Randomised 4-11s gaps (never a fixed
  interval), a 25-60s pause every ~12 requests, a proxy per request, doubling
  backoff, and a circuit breaker that stops after 5 consecutive failures — a
  failure streak means you are already blocked, and continuing escalates it.
- **`parse_client_info` is untested against real markup**: it could not be
  verified without a session, so it matches `"key": value` pairs with several
  aliases per field rather than depending on one JSON shape, and returns None
  per missing field instead of failing. Tests cover it via synthetic fixtures.
- **Hire rate works signed in, and the account pays for it**: `--login` proved
  it (89%, 17 posted, 15 with hires) — but that session was soft-blocked after
  ~12 page loads, with Upwork serving "We'll be right back" (HTTP 200, shell
  never hydrates). The anonymous profile kept working from the same IP in the
  same minute, so the throttle is account-scoped and proxies are irrelevant to
  it. Hence separate profiles and `--logged-in` being opt-in.
- **Three failure modes, told apart**: 403 Cloudflare challenge, 429 Upwork soft
  block, 503 loaded-but-never-rendered. They were all reported as "Cloudflare —
  a real browser is required", which sent diagnosis down the wrong path. A 429
  trips the breaker immediately; retrying a soft block extends it.
- **`live` is never a fallback verdict, and `--logged-in` never downgrades
  silently**: `session_state()` used to return `live` whenever it failed to
  recognise the page, so a freshly reset profile — no visitor nav yet because
  nothing had rendered yet — passed as signed in, and the run enriched
  anonymously into a CSV with an empty hire-rate column. Now: absent session
  cookies settle it as `signed_out` with no page load at all; `live` requires
  cookies *plus* a rendered page *plus* no visitor markup; an unrendered page
  or a challenge is `unknown`. On top of that, an interactive `--logged-in` run
  that cannot deliver hire rate asks what to do (sign in / reset then sign in /
  anonymous / stop) instead of choosing for the user, and a signed-in run that
  produced no hire rate at all says so after enrichment. Unattended runs
  (`--watch`, `--no-auto-login`, non-tty stdin) keep the old auto behaviour —
  there is nobody to ask.
- **Anonymously, hire rate is impossible, and the payload proves it**: the page's Nuxt state
  blob carries the denominator's field (`postedCount`) and Upwork ships it as
  null to visitors — every sibling stat is populated, that one and `openCount`
  are not (4 established clients, 2026-08-12). No amount of parsing recovers it.
  `avg_spend_per_hire` and `active_hire_share` are the computed stand-ins.
- **Parse the embedded payload, not just the rendered card**: the card is a
  lossy view of the page state. The payload has the star rating, review count
  and jobs-with-hires (none rendered logged out) and exact figures rather than
  "$326K". `parse_payload_stats` applies last so it wins over text scraping.
- **Payload indices resolve exactly ONE level**: values in the flat array are
  themselves small integers, so recursing reads a real value (9 active
  contracts) as an index and returns garbage. This bit once already.
- **Datacenter proxies do not work with Upwork**: all 10 on the configured
  Webshare account returned 200 from an IP-echo service and 403 from
  upwork.com (2026-08-12). Reaching the internet is not evidence a proxy is
  usable — `--check-proxies` tests against Upwork itself for that reason, and
  `_fetch_token` names the cause on a proxied 403 instead of burning retries.
  Residential proxies would be required; direct works today.
- **Explicit constructor args beat env config**: `WebshareProxyManager` takes
  env values only when neither argument is passed. Per-field fallback let an
  env API key silently override a URL the caller passed on purpose, and made
  the test suite depend on whatever `.env` held.
- **Country is enrichment-only, so the country filter lives after enrichment**:
  the search API cannot supply it at any price. Every country-bearing field on
  `PubJobSearchResult` — `client`, `clientCountry`, `location`, `buyer`,
  `attrs`, `clientRelation`, `upworkHistoryData` — and on `JobFullProfile`
  answers a visitor token with `doesn't have enough oauth2 permissions/scopes`
  (probed field by field, 2026-08-17; introspection is disabled for this
  client). So filtering cannot happen at fetch time, an excluded job still
  costs its ~8s enrichment before being dropped, and `--limit N` yields fewer
  than N. The CLI warns when the filter is armed without `--enrich-clients`
  rather than emitting an unfiltered file that looks filtered.
- **The excluded-country default is India/Pakistan/Bangladesh/Egypt/Philippines,
  and unknown countries are kept**: a failed page load is not evidence about
  where a client is, and dropping jobs on a browser timeout silently loses
  work. `--drop-unknown-country` opts into the strict reading. The list itself
  is a default, not a policy baked into the code — `--exclude-country`,
  `--allow-country`, `EXCLUDED_COUNTRIES` and `CountryFilter.from_names` all
  replace it.
- **Country matching is alias-based, not string equality**: the rendered card
  gives "India" while other markup paths give "IND" (real values seen: "AUS",
  "NLD", "USA"). Long name, ISO alpha-2, alpha-3 and official long forms
  collapse to one key; anything outside the table matches on its own normalised
  text, so a country can be excluded without a table entry.
- **`CountryFilter` has no imports from the rest of the package**: it takes a
  duck-typed job (`job.client.country`) rather than the `Job` model, so a
  downstream integration — n8n, a database writer, an API — can apply the same
  rules to its own records. `allows()` deliberately matches
  `Niche.is_relevant`'s signature so it drops into `scrape(job_filter=...)`.
- **The country filter runs inside `run_enrichment`, not after it**: the
  separate `--separate-clients` file is written there, and a client whose job
  was excluded appearing in it would contradict the main output. Filtering
  before that write keeps both files describing one set of jobs.
- **Retry once on token expiry**: `scrape()` invalidates the token and retries a
  single time, then raises. The original's controller looped forever instead —
  the equivalent here is `scrape_loop()`, which catches and backs off 30s.

## Testing

```bash
python -m pytest tests -q     # 435 tests, fully mocked, no network
```

`tests/test_job_models.py`, `test_token_manager.py` and `test_proxy_manager.py`
are ports of the original suite with rewritten imports. `test_job_fetcher.py`,
`test_scraper.py`, `test_cli.py` and `test_country_filter.py` cover what's new
here.
