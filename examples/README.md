# examples/ — starter files

Copy these to get from a fresh clone to a working dry-run. None of them contain real personal data.

| File | Copy it to | What it is |
|------|------------|------------|
| `jobs.sample.json` | `data/jobs.json` (or any folder you point `ARIA_CORE_DATA` at) | One sample job posting. The engine looks up jobs by `id` here. Replace with the real job(s) you're applying to. |
| `../apply_engine/applicant_profile.example.json` | `apply_engine/applicant_profile.json` | Your identity + contact + standard answers. The `apply` flow fills forms from this. Git-ignored once named `applicant_profile.json`. |
| `../apply_engine/voice_profile.example.md` | `apply_engine/voice_profile.md` | How you write — style/identity guidance for the LLM drafter. Git-ignored. |
| `../apply_engine/narrative.example.md` | `apply_engine/narrative.md` | Your identity story — the bedrock the drafter writes from. Git-ignored. |
| `holding.example.json` | `data/holding.json` | Unqualified job stubs that `qualify run` drains, enriches, scores, and promotes. |
| `fit_rubric.example.md` | `data/fit_rubric.md` | The 1–10 fit rubric `qualify` scores against. Generic — rewrite it to describe the candidate you're scoring for. |
| `contacts.example.json` | `data/contacts.json` | A networking CRM for `engage`'s contact-hygiene lane to repair. |
| `engage_config.example.json` | `data/engage_config.json` | `engage` lane flags (hygiene/staging/commit) + caps. Malformed → everything off. |

Quick start (from the repo root):

```bash
mkdir -p data
cp examples/jobs.sample.json data/jobs.json
cp apply_engine/applicant_profile.example.json apply_engine/applicant_profile.json
cp apply_engine/voice_profile.example.md       apply_engine/voice_profile.md
cp apply_engine/narrative.example.md           apply_engine/narrative.md
# then edit each with your own details and run:
python -m apply_engine --job JOB-001 --dry-run
```

The default data folder is `./data` at the repo root. Point somewhere else with the
`ARIA_CORE_DATA` environment variable.

## source — sourcing new postings

The `source` subcommand finds new job postings from public ATS feeds.

| File | Copy it to | What it is |
|------|------------|------------|
| `watchlist.example.json` | `data/ats_watchlist.json` (or your `ARIA_CORE_DATA` folder) | The companies `source scan` checks, and which ATS each uses. |

The watchlist is a JSON doc — either a bare list of entries or `{"entries": [...]}` —
where each entry is:

```json
{ "company": "Anthropic", "ats": "greenhouse", "slug": "anthropic" }
```

- `company` — display name, used for dedupe/reporting.
- `ats` — one of `greenhouse`, `lever`, `ashby`.
- `slug` — the company's board slug, i.e. the path segment in its public board URL
  (e.g. `boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`,
  `jobs.ashbyhq.com/<slug>`).

```bash
cp examples/watchlist.example.json data/ats_watchlist.json
# scan the watchlist for new keyword-matched postings -> markdown table + review queue
python -m apply_engine source scan
```

`source scan` keyword-filters titles (the default keyword set targets AI / forward-
deployed / applied-ML roles — edit `KEYWORD_PATTERNS` in `apply_engine/source/feeds.py`
for your own search), dedupes against `jobs.json`, and writes a timestamped review
queue to your data folder. It never writes `jobs.json` — review the queue and merge
selectively.

## qualify — turning stubs into actionable jobs

`qualify run` drains `holding.json`: for each stub it resolves a direct apply URL,
fetches the full JD, gates on enrichment (a real posting URL + a substantial JD), scores
fit against your rubric, and **promotes** the keepers into `jobs.json` with a `JOB-NNN`
id. It never drops a job for low fit — only dead links (after 3 tries).

```bash
cp examples/holding.example.json data/holding.json
cp examples/fit_rubric.example.md data/fit_rubric.md   # then rewrite it for your search
python -m apply_engine qualify run
# resolve one company+title to its direct apply URL (or "no confident match")
python -m apply_engine qualify resolve --company "Anthropic" --title "Forward Deployed Engineer"
```

Fit scoring shells out to `claude -p` against your rubric (no metered API). `qualify
resolve` fails closed: it only prints a URL on a confident title match against a real
board posting, never a guess.

## engage — the autonomous daily routine

`engage run` cleans the pipeline and stages work to the brink without putting a decision
on your critical path: every item lands in **A** (auto-done hygiene), **B** (staged for
your one click), or **C** (needs-work, parked). It journals every change and — only when
you pass `--commit` — makes one scoped git commit so any run is reversible with
`git revert`.

```bash
cp examples/contacts.example.json    data/contacts.json
cp examples/engage_config.example.json data/engage_config.json
python -m apply_engine engage run --dry-run   # plan + journal only, zero writes
python -m apply_engine engage run             # live hygiene + staging (never submits/sends)
```

The outreach (`warm-path`) lane ships as an inert stub — it returns
`outreach_not_configured` until you plug in your own contact-sourcing + email-verify
provider. Lane flags in `engage_config.json` fail safe: a malformed config turns
everything off.
