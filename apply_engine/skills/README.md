# apply_engine/skills

Markdown **skill** files — the reusable know-how the apply engine's headless `claude -p`
calls load via `--add-dir`, instead of prompts hard-coded inline in Python.

Each skill is self-contained instructions for one capability, so the same definition is used
by the scheduled routine, the headless engine, and an interactive session — defined once,
versioned here.

Planned (migration steps 2–5):
- `fit-score.md` — re-rate a job's fit (1–10) off the real JD
- `draft-answers.md` — draft custom-question answers (from `answer_gen` inline prompt)
- `audit-rules.md` — fabrication + quality audit rules (from `refresh_audit`/`quality_judge`)
- `recon.md` — lean company recon brief
- `fill-form.md` — how to drive a live application form (Workday/iCIMS quirks) for the form-driver agent

See `career/docs/APPLY_SYSTEM_MAP.md`.
