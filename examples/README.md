# examples/ — starter files

Copy these to get from a fresh clone to a working dry-run. None of them contain real personal data.

| File | Copy it to | What it is |
|------|------------|------------|
| `jobs.sample.json` | `data/jobs.json` (or any folder you point `ARIA_CORE_DATA` at) | One sample job posting. The engine looks up jobs by `id` here. Replace with the real job(s) you're applying to. |
| `../apply_engine/applicant_profile.example.json` | `apply_engine/applicant_profile.json` | Your identity + contact + standard answers. The `apply` flow fills forms from this. Git-ignored once named `applicant_profile.json`. |
| `../apply_engine/voice_profile.example.md` | `apply_engine/voice_profile.md` | How you write — style/identity guidance for the LLM drafter. Git-ignored. |
| `../apply_engine/narrative.example.md` | `apply_engine/narrative.md` | Your identity story — the bedrock the drafter writes from. Git-ignored. |

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
