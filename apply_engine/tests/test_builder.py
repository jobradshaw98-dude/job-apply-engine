# -*- coding: utf-8 -*-
"""Unit tests for the profile-driven resume/cover builder.

The `claude -p` call is the only non-deterministic step; everything else (prompt assembly,
JSON parsing, template fill, orchestration) is pure and tested here with an injected fake
generator and a stubbed renderer. No LLM, no browser, no network.
"""

import json
import pytest

from apply_engine.builder import generate


PROFILE = {
    "identity": {
        "name": "Sam Rivera",
        "email": "sam.rivera@example.com",
        "phone": "555-555-0100",
        "location": "Austin, TX",
        "links": {"linkedin": "https://linkedin.com/in/example", "github": "", "website": ""},
    },
    "headline": "Simulation Engineer",
    "work_history": [{"company": "Meridian", "role": "Engineer", "location": "Austin",
                      "start": "2022", "end": "Present", "bullets": ["Built 5 automation tools."]}],
    "education": [{"degree": "M.A.Sc.", "school": "State University", "year": "2022", "detail": ""}],
}

JOB = {"id": "JOB-001", "company": "Globex", "title": "Forward Deployed Engineer",
       "jd_text": "Work with customers to ship software. Python and automation a plus."}

CONTENT = {
    "resume": {
        "headline": "FDE — simulation + AI",
        "summary": "Engineer who ships automation.",
        "experience": [{"company": "Meridian", "role": "Engineer", "location": "Austin",
                        "dates": "2022-Present", "bullets": ["Built 5 automation tools.", "Led FEA."]}],
        "education": [{"degree": "M.A.Sc.", "school": "State University", "location": "Austin",
                       "date": "2022", "detail": "Thesis on polymers."}],
        "skills": [{"label": "Simulation", "items": ["LS-DYNA", "ANSYS"]},
                   {"label": "AI", "items": "Claude Code, Python"}],
    },
    "cover": {"addressee": "Hiring Team, Globex", "salutation": "Dear Hiring Team,",
              "para1": "I'm applying for the Forward Deployed Engineer role at Globex.",
              "para2": "At Meridian I built five automation tools.",
              "para3": "My edge is cross-domain breadth.", "para4": "I'd love to discuss."},
}


# ---- prompt assembly ----

def test_build_prompt_grounds_in_facts_and_jd():
    p = generate.build_prompt(PROFILE, "VOICE RULES HERE", JOB)
    assert "Forward Deployed Engineer" in p and "Globex" in p     # role + company
    assert "Built 5 automation tools." in p                       # facts embedded
    assert "Python and automation a plus." in p                   # JD embedded
    assert "VOICE RULES HERE" in p                                # voice embedded
    assert "Never invent" in p                                    # hard rule present
    assert '"resume"' in p and '"cover"' in p                     # schema present


def test_build_prompt_is_pure():
    assert generate.build_prompt(PROFILE, "v", JOB) == generate.build_prompt(PROFILE, "v", JOB)


# ---- parsing ----

def test_parse_plain_json():
    assert generate.parse_content(json.dumps(CONTENT))["resume"]["headline"] == "FDE — simulation + AI"


def test_parse_fenced_json():
    txt = "Here you go:\n```json\n" + json.dumps(CONTENT) + "\n```\nHope that helps!"
    assert generate.parse_content(txt)["cover"]["para1"].startswith("I'm applying")


def test_parse_prose_wrapped_json():
    txt = "Sure. " + json.dumps(CONTENT) + " Done."
    assert "resume" in generate.parse_content(txt)


def test_parse_empty_raises():
    with pytest.raises(ValueError):
        generate.parse_content("   ")


def test_parse_no_json_raises():
    with pytest.raises(ValueError):
        generate.parse_content("no json at all here")


def test_parse_missing_keys_raises():
    with pytest.raises(ValueError):
        generate.parse_content(json.dumps({"something_else": 1}))


# ---- contact line ----

def test_contact_line_joins_present_skips_empty():
    line = generate.contact_line(PROFILE["identity"])
    assert "sam.rivera@example.com" in line and "Austin, TX" in line
    assert "linkedin.com/in/example" in line
    assert line.count("|") == 3  # email, phone, location, linkedin -> 3 separators; github/website empty


# ---- template fill ----

def test_fill_resume_replaces_all_placeholders():
    html = generate.fill_resume(CONTENT, PROFILE["identity"])
    assert "{{" not in html                                   # every placeholder substituted
    assert "Sam Rivera" in html
    assert "Built 5 automation tools." in html and "Led FEA." in html
    assert "LS-DYNA, ANSYS" in html                           # list items joined
    assert "State University" in html


def test_fill_cover_replaces_all_placeholders():
    html = generate.fill_cover(CONTENT, PROFILE["identity"], date_str="June 24, 2026")
    assert "{{" not in html
    assert "June 24, 2026" in html
    assert "I'm applying for the Forward Deployed Engineer role at Globex." in html
    assert "Dear Hiring Team," in html


def test_fill_escapes_html():
    ident = {"name": "A&B <test>", "email": "x@y.z"}
    html = generate.fill_resume({"resume": {}}, ident)
    assert "A&amp;B &lt;test&gt;" in html and "<test>" not in html


# ---- orchestration (injected generator + stubbed renderer) ----

def test_generate_uses_injected_generator_and_writes_html(tmp_path, monkeypatch):
    captured = {}

    def fake_render(html_path, output_dir, name, applicant_name="", contact_token="", **kw):
        captured.setdefault("calls", []).append(
            {"name": name, "applicant_name": applicant_name, "contact_token": contact_token,
             "html": open(html_path, encoding="utf-8").read()})
        return {"pdf_path": f"{output_dir}/{name}.pdf", "page_count": 1,
                "checks": {"page_count_is_1": True}, "all_pass": True, "autofit_adjustments": 0}

    monkeypatch.setattr(generate.render, "auto_fit_one_page", fake_render)

    # inject the profile via path so we don't depend on a profile.json on disk
    pf = tmp_path / "profile.json"
    pf.write_text(json.dumps(PROFILE), encoding="utf-8")

    fake_gen = lambda prompt: json.dumps(CONTENT)
    results = generate.generate(JOB, tmp_path / "out", kinds=("resume", "cover"),
                                profile_path=str(pf), _generator=fake_gen)

    assert set(results) == {"resume", "cover"}
    names = [c["name"] for c in captured["calls"]]
    assert names == ["resume", "cover_letter"]
    # the renderer's identity-checks were parameterized from the profile (not hardcoded)
    assert all(c["applicant_name"] == "Sam Rivera" for c in captured["calls"])
    assert all(c["contact_token"] == "sam.rivera@example.com" for c in captured["calls"])
    assert "Built 5 automation tools." in captured["calls"][0]["html"]
