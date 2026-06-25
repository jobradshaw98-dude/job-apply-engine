"""engage — the autonomous career-ops orchestrator.

engage is the daily overlay that keeps the pipeline polished and action-ready
WITHOUT putting a human decision on the critical path. Every item it touches
resolves into exactly one autonomy bucket, chosen by the agent, never escalated:

  A  auto-commit   deterministic hygiene (schema repair, normalize, follow-up cadence)
  B  auto-stage    a sourced/verified contact or staged application sits at the BRINK
                   (one human click away). The agent NEVER sends or submits.
  C  needs-work    below-confidence / unverifiable -> a passive bucket, no push.

The novel idea: "I'm not sure" is a terminal state the agent OWNS. Uncertainty
routes to bucket C and the run continues — the human's decision is never on the
critical path.

Reversibility spine: one journal entry per change -> a per-run journal file ->
(optionally) exactly one git commit per live run, so a whole run is revertible
with `git revert`. `--dry-run` writes the journal only and makes zero changes.

What ships here vs. what is stubbed:
  * runner.py            — the reversibility spine + A/B/C orchestrator (PORTED, runnable)
  * crm_util.py          — canonical, collision-safe CRM writers (PORTED, runnable)
  * contact_hygiene.py   — the deterministic repair lane (PORTED, runnable); the
                           LLM LinkedIn-sourcing lane keeps an injectable runner
                           seam but is OFF by default (no model shell-out).
  * warm_path.py         — a STUB / documented hook. Plug in your own contact
                           sourcing + email verification here. Ships disabled.
"""
