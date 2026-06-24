"""generate.py — profile-driven resume + cover-letter generator.

Pipeline: profile facts + voice + a target JD  ->  one `claude -p` call returns structured
content  ->  fill the HTML template  ->  render to a one-page PDF.

The drafter GROUNDS every claim in the profile facts and never invents tools, numbers,
employers, or outcomes. The functions are split so the prompt assembly, JSON parsing, and
template fill are all unit-testable without a live LLM (the `claude -p` call is the only
non-deterministic step).
"""

import html
import json
import re
from pathlib import Path

from .. import llm
from . import render

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
PROFILE_JSON = HERE / "profile.json"            # user-supplied (git-ignored)
PROFILE_EXAMPLE = HERE.parent.parent / "examples" / "profile.example.json"
VOICE = HERE.parent / "voice_profile.md"        # reuse the engine's voice profile if present
VOICE_EXAMPLE = HERE.parent / "voice_profile.example.md"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load_profile(profile_path=None) -> dict:
    """Load the applicant profile. Explicit path > profile.json > bundled example (with a warning)."""
    import sys
    p = Path(profile_path) if profile_path else PROFILE_JSON
    if not p.exists():
        if PROFILE_EXAMPLE.exists():
            print(f"[builder] no profile at {p}; using the bundled EXAMPLE ({PROFILE_EXAMPLE.name}). "
                  f"Copy it to {PROFILE_JSON} and fill in your own facts.", file=sys.stderr)
            p = PROFILE_EXAMPLE
        else:
            raise FileNotFoundError(f"No profile found at {p} and no bundled example available.")
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("_comment", None)
    return data


def load_voice() -> str:
    for v in (VOICE, VOICE_EXAMPLE):
        try:
            return v.read_text(encoding="utf-8")
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# prompt assembly  (pure — unit-testable)
# ---------------------------------------------------------------------------

_SCHEMA = """{
  "resume": {
    "headline": "one short headline line tailored to this role",
    "summary": "2-3 sentence professional summary, grounded in the facts, angled at this JD",
    "experience": [
      {"company": "", "role": "", "location": "", "dates": "",
       "bullets": ["tailored bullet grounded ONLY in the provided facts", "..."]}
    ],
    "education": [{"degree": "", "school": "", "location": "", "date": "", "detail": ""}],
    "skills": [{"label": "category", "items": "comma-separated, JD-relevant first"}]
  },
  "cover": {
    "addressee": "e.g. Hiring Team, <Company>",
    "salutation": "Dear Hiring Team,",
    "para1": "role hook + why-this-company (name the role and company in the first sentence)",
    "para2": "core evidence from the facts with a concrete, true detail",
    "para3": "the differentiator - cross-domain breadth, AI-native delivery",
    "para4": "short warm close offering to discuss"
  }
}"""


def build_prompt(profile: dict, voice: str, job: dict) -> str:
    """Assemble the single generation prompt. Pure: same inputs -> same string."""
    jd = (job.get("jd_text") or "").strip()
    company = job.get("company") or job.get("employer") or "the company"
    role = job.get("title") or job.get("role") or "the role"
    return (
        "You are drafting the applicant's OWN tailored resume and cover letter for a specific job. "
        "Write in their voice, first person for the cover letter.\n\n"
        "HARD RULES (never break):\n"
        "- Ground EVERY claim ONLY in the PROFILE FACTS below. Never invent a tool, number, "
        "employer, title, date, metric, or outcome that is not in the facts.\n"
        "- Do not claim experience with a JD requirement that the facts don't support. Scope to "
        "what is genuinely there, or leave it out.\n"
        "- Tailor by SELECTING and REPHRASING the most role-relevant true facts, not by adding new ones.\n"
        "- Resume: express impact as outcomes; keep bullets tight. Cover: four short paragraphs, "
        "at most one em-dash total, no 'I am excited to apply' opener.\n\n"
        f"TARGET ROLE: {role} at {company}\n\n"
        "# VOICE & CRAFT (style guidance, NOT a source of facts)\n" + (voice or "(none provided)") + "\n\n"
        "# PROFILE FACTS (the ONLY source of truth about the applicant)\n"
        + json.dumps(profile, indent=2, ensure_ascii=False) + "\n\n"
        "# JOB DESCRIPTION (for tailoring/relevance only, NOT a source of facts about the applicant)\n"
        + (jd or "(no JD text provided - tailor from the role/company name only)") + "\n\n"
        "Return ONLY a single JSON object, no prose before or after, matching EXACTLY this shape:\n"
        + _SCHEMA + "\n"
    )


# ---------------------------------------------------------------------------
# parsing  (pure — unit-testable)
# ---------------------------------------------------------------------------

def parse_content(text: str) -> dict:
    """Extract the JSON object from the model's reply. Tolerates ```json fences and surrounding prose."""
    if not text or not text.strip():
        raise ValueError("empty generation output")
    # strip code fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        # fall back to the outermost {...}
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON object found in generation output")
        candidate = text[start:end + 1]
    data = json.loads(candidate)
    if "resume" not in data and "cover" not in data:
        raise ValueError("generation output missing both 'resume' and 'cover' keys")
    return data


# ---------------------------------------------------------------------------
# template fill  (pure — unit-testable)
# ---------------------------------------------------------------------------

def _h(s) -> str:
    # quote=False: every placeholder lands in an HTML *text node* (never an attribute value),
    # so escaping &<> is enough; leaving quotes/apostrophes intact keeps the prose clean.
    return html.escape(str(s or ""), quote=False)


def contact_line(identity: dict) -> str:
    links = identity.get("links", {}) or {}
    parts = [identity.get("email"), identity.get("phone"), identity.get("location"),
             links.get("linkedin"), links.get("github"), links.get("website")]
    return "  |  ".join(_h(p) for p in parts if p)


def _experience_html(experience: list) -> str:
    out = []
    for e in experience or []:
        bullets = "".join(f"<li>{_h(b)}</li>" for b in (e.get("bullets") or []))
        out.append(
            '<div class="role-row"><div class="role-left">'
            f'<span class="role-title">{_h(e.get("role"))}</span>'
            f'<span class="role-loc">  {_h(e.get("company"))}'
            f'{", " + _h(e.get("location")) if e.get("location") else ""}</span>'
            f'</div><div class="role-date">{_h(e.get("dates"))}</div></div>'
            f'<ul class="bl">{bullets}</ul>'
        )
    return "\n".join(out)


def _education_html(education: list) -> str:
    out = []
    for e in education or []:
        detail = f'<div class="edu-detail">{_h(e.get("detail"))}</div>' if e.get("detail") else ""
        out.append(
            '<div class="edu-row">'
            f'<div class="edu-degree">{_h(e.get("degree"))}</div>'
            f'<div class="edu-date">{_h(e.get("date"))}</div></div>'
            f'<div class="edu-school">{_h(e.get("school"))}'
            f'{", " + _h(e.get("location")) if e.get("location") else ""}</div>'
            f'{detail}'
        )
    return "\n".join(out)


def _skills_html(skills: list) -> str:
    out = []
    for s in skills or []:
        items = s.get("items")
        if isinstance(items, list):
            items = ", ".join(items)
        out.append(f'<div class="skill-row"><span class="skill-label">{_h(s.get("label"))}:</span> {_h(items)}</div>')
    return "\n".join(out)


def fill_resume(content: dict, identity: dict) -> str:
    tpl = (TEMPLATES / "resume_template.html").read_text(encoding="utf-8")
    r = content.get("resume", {})
    return (tpl
            .replace("{{NAME}}", _h(identity.get("name")))
            .replace("{{CONTACT_LINE}}", contact_line(identity))
            .replace("{{HEADLINE}}", _h(r.get("headline") or identity.get("headline")))
            .replace("{{SUMMARY}}", _h(r.get("summary")))
            .replace("{{EXPERIENCE_BLOCKS}}", _experience_html(r.get("experience")))
            .replace("{{EDUCATION_BLOCKS}}", _education_html(r.get("education")))
            .replace("{{SKILLS_BLOCKS}}", _skills_html(r.get("skills"))))


def fill_cover(content: dict, identity: dict, date_str: str = "") -> str:
    tpl = (TEMPLATES / "cover_letter_template.html").read_text(encoding="utf-8")
    c = content.get("cover", {})
    return (tpl
            .replace("{{NAME}}", _h(identity.get("name")))
            .replace("{{CONTACT_LINE}}", contact_line(identity))
            .replace("{{DATE}}", _h(date_str))
            .replace("{{ADDRESSEE}}", _h(c.get("addressee")))
            .replace("{{SALUTATION}}", _h(c.get("salutation") or "Dear Hiring Team,"))
            .replace("{{PARA1}}", _h(c.get("para1")))
            .replace("{{PARA2}}", _h(c.get("para2")))
            .replace("{{PARA3}}", _h(c.get("para3")))
            .replace("{{PARA4}}", _h(c.get("para4"))))


# ---------------------------------------------------------------------------
# orchestration  (integration — needs claude -p + a browser)
# ---------------------------------------------------------------------------

def generate(job: dict, out_dir, kinds=("resume", "cover"), profile_path=None,
             model: str = "sonnet", date_str: str = "", _generator=None) -> dict:
    """Draft + render the requested document kinds for `job`. Returns {kind: render_result}.

    `_generator` is an injection seam for tests: a callable(prompt)->str. In production it
    defaults to the engine's `claude -p` wrapper (subscription only, no API)."""
    profile = load_profile(profile_path)
    identity = profile.get("identity", {})
    voice = load_voice()
    prompt = build_prompt(profile, voice, job)

    gen = _generator or llm.make_claude_llm(model)
    content = parse_content(gen(prompt))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = identity.get("name", "")
    contact_token = identity.get("email", "")
    results = {}

    if "resume" in kinds:
        rp = out_dir / "resume_src.html"
        rp.write_text(fill_resume(content, identity), encoding="utf-8")
        results["resume"] = render.auto_fit_one_page(
            str(rp), str(out_dir), name="resume", applicant_name=name, contact_token=contact_token)
    if "cover" in kinds:
        cp = out_dir / "cover_src.html"
        cp.write_text(fill_cover(content, identity, date_str), encoding="utf-8")
        results["cover"] = render.auto_fit_one_page(
            str(cp), str(out_dir), name="cover_letter", applicant_name=name, contact_token=contact_token)

    return results
