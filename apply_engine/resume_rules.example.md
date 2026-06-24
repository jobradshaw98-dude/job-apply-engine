# Resume & portfolio-deck generation rules (EXAMPLE template)

> **This is an EXAMPLE template.** Copy it to `resume_rules.md` (git-ignored); the engine prefers
> your real `resume_rules.md` and only falls back to this example when it is absent. Accepted rules
> are appended by `/career-learn`; this starter file ships empty.

Durable rules learned from the applicant's resume and portfolio-slide corrections. This is the
resume/deck counterpart to `voice_profile.md` (which holds cover-letter and answer voice).

Rules land here only through the human-gated `/career-learn` command, after the user reviews a
proposal from `distill_corrections.py` and accepts it. Nothing is auto-appended.

WIRED: `apply_engine/llm.py::load_facts` reads this file into the grounding context for every
generation (resume builds + regen_content edits + answer drafting), labeled as style/format
guidance — so accepted rules take effect on the next build/edit, same as voice_profile.md does
for cover/answers. (regen_slide deck prompts do not read it yet — that wiring is still open.)

## Accepted rules

<!-- /career-learn appends accepted rules below this line, newest last. -->
