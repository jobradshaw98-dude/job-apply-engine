# Apply Engine — stage-to-brink form automation

[![CI](https://github.com/jobradshaw98-dude/aria/actions/workflows/ci.yml/badge.svg)](https://github.com/jobradshaw98-dude/aria/actions/workflows/ci.yml)
&nbsp;**1,057 tests** · **~74% line coverage** · offline, no API key needed

A headless engine that drives multi-step web application forms across several
backends, fills every field from a structured profile, drafts free-text answers
with quality gates, and **stages the form to the submit brink — then stops.** It
never clicks submit. A human does the final review and the final click.

This is the "ships real, tested product" half of the [ARIA](../README.md) showcase.
Everything here runs on a **fictional sample applicant** ("Sam Rivera") — no real
personal data.

> Design stance: an LLM is great at *drafting and reasoning* and untrustworthy as a
> *gate*. So every decision that must be correct — is this field filled? is the
> applicant work-authorized? is this answer on-target? is it safe to submit? — is
> **deterministic, tested code**. The LLM drafts; the gates decide.

## What it does

- **Multiple ATS backends.** Adapter per backend (Greenhouse, Lever, Ashby,
  Workday) plus a **generic fallback** that fills forms it's never seen. Adding a
  backend = one adapter implementing the base interface.
- **Fills the whole form.** Standard fields from the profile; messy widgets
  (React-Select, custom dropdowns, radio yes/no, location autocompletes) handled
  explicitly, not assumed.
- **Drafts free-text answers** in the applicant's voice, then runs them through
  length, accuracy, and on-target gates before they're allowed to stand.
- **Enforces policy deterministically.** Work-authorization and office/relocation
  answers follow fixed rules; a compliance layer refuses to fabricate.
- **Never submits.** A submit-integrity gate verifies the *tailored* (not master)
  documents are attached, content edits force a re-gate, and submission is left to
  the human. (Automated final-submit also trips bot detection — another reason to
  stop at the brink.)

## Architecture

```
job → ats_detect ──▶ adapter (greenhouse|lever|ashby|workday|generic)
                          │
                          ▼
                    form_spec  ──▶  field_map ──▶ converge (fill + verify loop)
                          │                              │
                  answer_gen (LLM draft)         deterministic gates:
                          │                       completeness · compliance ·
                          ▼                       screening · submit-integrity
                    quality gates                        │
                          └──────────────▶ finish: stage to brink, NEVER submit
```

Deterministic gates are plain Python with tests; the LLM is confined to drafting
inside `answer_gen` / `llm`. State for a run is tracked so a crash mid-fill is
recoverable, and concurrent runs are merge-safe.

## Test suite

**1,057 tests passing, ~74% line coverage** (offline — no API key, network, or
browser needed; the suite parses HTML fixtures of real ATS forms). Run it:

```bash
cd apply-engine
python -m venv .venv
# Windows (use .venv/bin/python on macOS/Linux):
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pytest tests/ -q
# reproduce the coverage figure:
.venv/Scripts/python.exe -m pytest tests/ --cov=. -q
```

`requirements.txt` marks `pywin32` (a Windows-only dependency of the live browser
layer) with an environment marker, so `pip install -r requirements.txt` works on
macOS/Linux too — it just skips that one package, and the offline suite still runs.

A handful of tests depend on modules that live in the original private monorepo and
are intentionally not published here; they're skipped via `pytest.ini`, which
documents exactly which and why. CI runs this suite on every push — see
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

## Layout

| Path | Role |
|---|---|
| `adapters/` | One module per ATS backend + generic fallback |
| `converge.py` | Fill-and-verify loop until the form is complete |
| `answer_gen.py`, `llm.py` | LLM drafting of free-text answers |
| `quality_judge.py`, `completeness.py`, `compliance.py`, `screening.py` | Deterministic gates |
| `finish.py` | Submit-integrity check + stage-to-brink (never submits) |
| `tests/` | 100+ test files; `tests/fixtures/` real-ATS HTML |
| `applicant_profile.example.json` | Template profile (fill in your own) |
