---
name: fill-form
description: How to drive a live job-application form to the SUBMIT BRINK and stop — field conventions, the hard work-auth/office/screening answer policies, file-upload rules, ATS quirks (Workday/iCIMS), verify-before-done, and the absolute never-submit invariant. Loaded by the form-driver agent.
---

# Fill a job-application form to the brink (NEVER submit)

You fill a staged application form for **the applicant** using their already-built, already-audited
package (tailored resume + cover + answers). You drive the browser, adapt to the live page, fill
every field, **verify**, and **STOP at the submit brink**. You do not click submit. Ever.

## The absolute invariant — NEVER SUBMIT

- Fill to the point where only "Submit" remains, then STOP and report. The user reviews and submits.
- This is not just a safety preference: **many score-gating ATSes** use **reCAPTCHA v3** (silent, no
  challenge). Automation scores low → the backend bot-flags an automated submit → the application is
  rejected; the user's own browser (real Google session) scores high enough. So an automated submit can
  *burn the application*. Stop at the brink; the user submits.
- **Advancing a multi-step form is NOT submitting.** Intermediate buttons — "Next", "Save &
  Continue", "Continue" — are allowed; they move between wizard steps. The FINAL action that submits
  the whole application is forbidden. On a **Review / final** step, STOP — do not click its primary
  button even if the page looks incomplete. If a button's label is ambiguous between advance and
  submit, **treat it as submit and STOP**.
- If you ever cannot tell whether an action would submit, STOP and ask. Never guess toward submit.

## Hard answer policies (encode these exactly — do not improvise)

**Work authorization / visa / sponsorship** — always clear the screen with NO immigration red flag:
- "Authorized to work in the US?" → **Yes**.
- "Will you now or in future require sponsorship (H1B/visa)?" → **No**.
- Combined "authorized to work WITHOUT requiring sponsorship?" → **Yes** (affirmative). Do NOT halt.
- "Are you a US citizen?" / nationality (factual binary) → answer **truthfully**. Never claim citizenship the applicant does not hold. Only surface to the user if the form forces a free-text explanation.
- **NEVER** surface green-card status, marriage, or any visa/AOS nuance in any field. That context is private and handled by the user with a human later.

**Office / onsite / RTO / relocation** → **Yes** unless the role itself is remote (then moot):
- "Commit to office Nx/week?", "Work onsite?", "Open to relocation?", "Willing to relocate to <city>?" → **Yes**.
- These are screen-out gates, not negotiations — the user sorts real logistics with a recruiter at offer stage.

**Negation = STOP, do not compute the answer.** On ANY inverted / negated phrasing of a work-auth,
office, or relocation, or screening question ("UNABLE/UNWILLING to relocate?", "do you NOT require
sponsorship?", "do you LACK 3+ years?"), do **NOT** auto-answer by mentally flipping the polarity —
a double-negative miss on these exact fields is the highest-stakes error here. **Surface it to
the user and let them answer.** Over-escalation is the designed-safe failure; never reason your way to
a favorable polarity on a negated high-stakes question.

**Screening Yes/No qualifiers** (skills, years, "have you done X?") → answer **truthfully** from the
applicant's grounded capabilities (`apply_engine/capabilities.md`): map each qualifier to a Clear
YES / Clear NO / PARTIAL line in that file; never claim hand-coding fluency the applicant does not
have. PARTIAL items → escalate to the user, don't fabricate a Yes. Watch
negation ("do you LACK 3+ yrs?") — never auto-answer a disqualifying Yes.

**EEO / demographics** → decline to self-identify (or "prefer not to say") unless the user says otherwise.
**Salary** → never enter a figure; leave blank or escalate (no comp in writing during a live process).

## File uploads (HARD)

- Upload filenames must be the applicant's canonical doc names — **`<APPLICANT>_Resume.pdf`** and
  **`<APPLICANT>_Cover_Letter.pdf`** (the applicant's name + doc type)
  verbatim — the filename is recruiter-visible. Never rename to `Company_Resume.pdf` / `resume.pdf`.
- The built docs live in `career/applications/APP-NN-*/`. If the browser upload sandbox restricts
  paths (Playwright MCP only allows under `~/projects/aria/`), COPY the file there keeping the
  canonical name; if two jobs' docs must coexist, use a per-job SUBFOLDER, never a renamed file.
- After uploading, **poll for the filename to render** in the form (it can appear async ~0.6–2s) —
  do not declare "attached" on a fixed wait; confirm the rendered filename.

## ATS quirks

- **Workday** (`*.myworkdayjobs.com`): multi-step wizard, often gated by a Create-Account / Sign-In
  step that varies per employer tenant. Use the existing `apply_engine/adapters/workday.py` as the
  fast-path (it handles login, the account gate, per-tenant creds, and walks to the Review brink).
  When the adapter stalls on a tenant variation, take over agentically: read the page, fill the
  step, advance — but STOP at Review. Account creation at an employer is **outward-facing** → that
  is the user's gate; do not create an account autonomously without their go.
- **iCIMS / Workable / custom**: no public board; drive agentically, same policies, stop at brink.
- **Greenhouse / Lever / Ashby**: the deterministic adapters already fill these fast — prefer them;
  only take over agentically if an adapter errors.

## Verify before "done" (never report success on zero work)

Before reporting the form ready: re-read every field you filled and confirm it holds the intended
value; confirm the resume + cover show as attached (by rendered filename); confirm no REQUIRED field
is empty; list any field you could not answer (PARTIAL screening, forced explanation, salary) for
the user. If you filled nothing or the page never showed the form, report that honestly — never claim
a fill that didn't land. Validate against the LIVE DOM, not an assumption about the layout.

## Persist the result — use the canonical writer, NEVER hand-write the manifest (HARD)

When you stop at the brink, record the staged result into the apply-queue manifest by calling the
canonical writer — **do not hand-edit `staged_applications.json` and never dump prose lines into it.**
The dashboard renders `custom_qs` / `work_auth` / `uploaded_docs` as lists of DICTS; a stray string
in any of them 500s the whole Apply Queue (JOB-163 Edwards, 2026-06-20). The writer validates the
shapes and fails loud if you pass the wrong type.

Write a JSON file of the result, then run it through the engine venv:

```bash
.venv/Scripts/python.exe -m apply_engine.form_driver_stage --json /tmp/result.json
```

The JSON uses these exact shapes (mirrors what the deterministic engine writes):

```json
{
  "job_id": "JOB-NNN", "company": "...", "role": "...", "url": "...", "ats": "workday",
  "status": "ready_to_submit",
  "filled_fields": ["step1: country=United States", "step3 Q1 18-or-older=Yes"],
  "custom_qs":     [{"q": "Desired salary", "kind": "text", "status": "answered", "reason": "", "value": "125000", "answered_by": "sam"}],
  "work_auth":     [{"q": "Authorized to work in the US without sponsorship?", "field": "authorized_no_sponsorship", "answer": "Yes"}],
  "uploaded_docs": [{"doc": "resume", "path": "C:\\...\\APPLICANT_Resume.pdf", "name": "APPLICANT_Resume.pdf"}]
}
```

`filled_fields` is the one field that takes plain strings (its renderer expects strings). Everything
in `custom_qs` / `work_auth` / `uploaded_docs` must be a dict with the keys shown. If the command
prints `STAGE FAILED (shape error...)`, fix the JSON shape — do NOT fall back to writing the file yourself.

**The command auto-runs the accuracy review.** After writing the record it runs the SAME honesty
review the deterministic engine runs (`accuracy review: PASS` / `BLOCKED ...` is printed). This is
the gate that lets the package be submitted — a form-driver stage now gets the identical check an
engine stage gets. If it prints `BLOCKED`, the answers have an accuracy problem: fix them and
re-stage; do not hand-edit the verdict. Submit stays locked until the review is `PASS`.

## Report (when you stop at the brink)

Return: ATS, URL, every field filled + its value, attachments confirmed, any field escalated to
the user and why, an explicit `submitted: false` with `reached: review-brink`, and confirmation that
you staged the result via `form_driver_stage` (the writer's success line).
