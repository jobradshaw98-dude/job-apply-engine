# Capability facts (EXAMPLE — fictional applicant "Sam Rivera")

> **This is a filled-in EXAMPLE for the fictional applicant Sam Rivera.** Copy it to
> `capabilities.md` (git-ignored) and replace every fact below with your own truthful Yes/No
> grounding. Keep the classifier rules and the section structure — those are general; swap out the
> identity-bound facts. The engine prefers your real `capabilities.md` and only falls back to this
> example (with a warning) when it is absent.

This is the ONLY source the screening classifier (`screening.py`) may use to answer a binary
Yes/No screening qualifier on a job application. It exists so the engine can truthfully answer
clear qualifiers ("3+ years experience?", "deployed AI in production?") instead of punting every
one to the user — WITHOUT ever fabricating a Yes.

Rules for the classifier:
- Answer **YES** when a "Clear YES" line below covers the question — answer it **confidently**,
  do not second-guess a clearly-supported Yes. If a question bundles a covered claim with an
  UNcovered one (e.g. "strong Python AND distributed-systems design?"), the uncovered part is not
  supported → ESCALATE.
- Answer **NO** when a "Clear NO" line supports a No.
- ESCALATE only for: something genuinely **not covered** here, a self-assessment/judgment call
  ("are you an expert in X?"), a **PARTIAL** item, or unaided coding fluency (see HARD RULE).
  Never fabricate a Yes — but a Yes that a Clear-YES line plainly supports is NOT a guess.

## Coding / software development — AI-native framing (Sam 2026-06-09; refines claims_ledger.md)

Sam builds and ships production software (primarily Python) by driving LLM CODING HARNESSES
(Claude Code, Codex) — he architects, directs, debugs, and OPERATES the systems in production; the
implementation is AI-generated. This is real, assertable engineering output, not hand-coded-from-
memory syntax recall. Answer coding questions truthfully on that basis — do NOT escalate them:
- "experience with / worked in / use / build in Python (or another language)?" → **YES.**
- "proficient in Python / strong software development / coding skills?" → **YES** (production output
  via AI harnesses; he ships and operates it).
- "fluent / expert WITHOUT AI assistance / from scratch / on a whiteboard / unassisted?" → **NO**
  (that is unaided syntax mastery, which he does not claim).
- In FREE-TEXT answers and documents, always frame it as **AI-native / AI-orchestrated development**
  (see `voice_profile.md` "AI-native engineering"): lead with shipped systems (Meridian DevBot — 5
  production tools; ARIA), own the full loop (architecture → agent/prompt design → debug →
  production operation), and position orchestrating AI as the high-leverage way modern eng ships.
  Never claim unaided hand-coding fluency; never name MATLAB as a personal coding skill.

## His ACTUAL AI / agent stack (name THESE; never recommend a tool he hasn't used)

When a JD names a specific framework/tool, map the answer to his REAL equivalent below — do NOT
suggest he add or claim the JD's tool if it isn't here. His edge is the systems he's built, not a
named library.
- **LLM coding harnesses:** Claude Code, Codex (his primary build tooling).
- **Agent systems:** multi-agent orchestration + agent-to-agent handoff (ARIA: career / finance /
  research / supervisor agents); production agent lifecycle (staging, gating, monitoring).
- **Integration / patterns:** MCP (Model Context Protocol) tool integration; tool-calling;
  structured / typed outputs; RAG-style retrieval; prompt/eval loops.
- **Supporting:** Python (AI-orchestrated), Flask, Playwright, Git, FEA/optimization tooling.
- **NOT used (never claim, never "add to the resume"):** LangChain, LangGraph, Langfuse,
  Pydantic-AI, and other named agent frameworks; specific cloud certs; Kubernetes-at-scale. If a JD
  asks for these, the honest move is to name his Claude Code / Codex / MCP orchestration as the
  equivalent and let it stand on the systems he's shipped — NOT to claim the named tool.

## Clear YES (truthful, grounded — safe to assert)

- **Designed / built agentic or LLM-based applications** — YES. ARIA is a multi-agent LLM system
  he architected and operates (career, finance, lead-gen agents).
- **Deployed / shipped AI agents or LLM tools to production** — YES. At Meridian R&D he built and
  rolled out 5 production Codex automation tools (test pipeline, root-cause agent, multi-objective
  optimization, FEA report generation, enablement frameworks).
  - **TIMELINE — HARD, do not fabricate.** The Codex/AI-automation work at Meridian began in
    **March 2026 — a few months ago, NOT years.** His Meridian *tenure* is 2023–Present (~2.5 yrs),
    but the AI-agent building is recent. NEVER say "two years / over the past couple of years / for
    years" about the Codex/agent work. If a question asks how long he's been building AI agents/
    automation, the honest answer is **"since early 2026 / the last several months"** (and ARIA,
    his personal multi-agent platform, is also 2026 — months, not years). Conflating Meridian tenure
    with the AI-tooling duration is the specific fabrication to avoid.
- **Shipped and operated production software** — YES, confidently. Both Meridian DevBot tooling and
  ARIA run in production and are maintained/operated by him. (How he builds it — AI-orchestrated —
  does not change that software shipped and runs; the coding-skill nuance lives in the HARD RULE,
  it does NOT make this question a maybe.)
- **Deployed AI / agents / automation to production** — YES, confidently. Meridian DevBot (5 tools)
  + ARIA. (A question naming a SPECIFIC framework he hasn't used — "...using LangChain/LangGraph?" —
  is framework-specific → ESCALATE; the bare "deployed AI in production?" is YES.)
- **3+ years (or 2+/4+/5+) of relevant professional experience** — YES. 5+ years effective
  (R&D engineer at Meridian + MASc research counts as relevant experience).
- **Bachelor's / undergraduate degree** — YES. Plus a MASc (Master of Applied Science),
  Mechanical Engineering, State University.
- **Master's / graduate degree** — YES. MASc Mechanical Engineering, State University.
- **Finite element analysis (FEA) / structural simulation** — YES. Core expertise (LS-DYNA +
  HyperWorks at Meridian; ANSYS in graduate research).
- **Design / numerical optimization, test-to-simulation correlation** — YES. Core expertise.
- **Authorized to work in the US** — YES (TN visa). NOTE: work-authorization, sponsorship, visa
  and citizenship questions are NOT answered here — they are owned by `work_auth.py`. The
  screening classifier must ESCALATE them; this line is context only.

## Clear NO (truthful)

- **PhD / doctorate** — NO. Highest degree is a Master's (MASc).
- **Active security clearance** — NO. (Also a sensitive class — ESCALATE.)
- **Professional Engineer (PE) license** — NO.

## PARTIAL / ESCALATE (true in part — do NOT auto-answer)

- **Customer-facing / client-facing delivery experience (FDE-style)** — PARTIAL. Internal
  stakeholders and POCs, not external paying customers. ESCALATE.
- **Cloud infrastructure at scale / Kubernetes / specific cloud certs** — PARTIAL. ESCALATE.
- **Specific named framework / tool depth not listed above** — ESCALATE unless clearly covered.
- **People-management / managed a team of N reports** — PARTIAL (leadership in sport/school/work,
  not formal direct reports). ESCALATE.
