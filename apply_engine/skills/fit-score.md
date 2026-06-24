---
name: fit-score
description: Re-rate a job's fit for Sam Rivera (1-10) from the real job description. Used after enrichment overwrites a thin JD with the matched posting's full text (job carries fit_stale=true).
---

# Fit-score a job for Sam Rivera

Given a job's **title + full job description**, output a fit score **1–10** and a one-line reason.
Score off the REAL JD — this runs precisely because a fuller, more accurate description just arrived.

## Who Sam is (the target function)

- R&D / Product Development Engineer at Meridian Devices. MASc Mechanical Engineering (State University).
- **AI-native builder + domain expert**: orchestrates AI coding agents (Codex at Meridian — 5 production tools; Claude Code for his ARIA system). Does NOT hand-code; frame as AI-orchestrated, never "fluent in Python."
- ~5+ years effective experience (MASc research counts). Senior-eligible, not junior.
- Simulation/optimization/test-to-sim strength (LS-DYNA, FEA, HyperWorks). Not CAD-led.
- Based Austin, TX. TN visa (Canadian). **ITAR / security-clearance roles = hard 0** (not eligible).

## Scoring rubric

| Score | Meaning |
|---|---|
| **9–10** | AI-native role he's built toward: Forward Deployed Engineer / Applied AI Engineer / Solutions Engineer / agentic-tooling / TechBio, with real agent/LLM-building + deployment-facing work. Strong company. |
| **7–8** | (a) AI-engineering-adjacent (AI-enabled R&D, ML-adjacent product) OR (b) strong TRADITIONAL fit — R&D / product-development / mechanical / medical-device / hardware-product engineering at his level. |
| **5–6** | Partial fit: real overlap but off on one axis — wrong seniority (junior/manager), thin AI signal, weak domain match, or a lateral with no clear step up. |
| **3–4** | Weak: mostly-different role, heavy skills he lacks, pure-sales/non-engineering, or requires relocation to a low-interest metro for a marginal role. |
| **1–2** | Disqualifying: ITAR/clearance-required, wrong field entirely, or a role he can't legally/credibly hold. |

## Adjustments

- **AI-native signal is the biggest lever** — explicit agent-building / LLM-deployment / FDE pushes toward 9–10.
- **Cast a wide net** on borderline traditional roles (PM, project engineer, pharma R&D are valid) — don't over-filter; when unsure between two bands, take the higher.
- **Down-rank**: ITAR/clearance (→ ≤2), pure sales with no engineering, IC→junior drops, and same-title/same-level laterals with no comp/scope step up.
- Location: San Diego / Carlsbad / North County or remote = neutral-to-plus; relocation to a low-interest metro = small minus, not disqualifying.

## Output (STRICT)

Return ONLY one JSON object, nothing else:

```json
{"fit_score": 8, "reason": "AI-native FDE at an agent-tooling startup; deployment-facing, senior-level — squarely his target."}
```

`fit_score` is an integer 1–10. `reason` is one sentence. No prose around the JSON.
