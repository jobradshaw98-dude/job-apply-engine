"""Hermetic tests for the deterministic contact-hygiene lane + the injectable-runner
LinkedIn sourcing. NO network, NO model shell-out — `source_linkedin` is exercised
only with an injected stub runner.
"""
from datetime import datetime

from apply_engine.engage import contact_hygiene as H


TODAY = datetime(2026, 6, 25)


# ── repair_followups (deterministic, in-place) ────────────────────────────────
def test_repair_sets_missing_followup_from_cadence():
    contacts = [{"id": "CON-1", "status": "contacted", "last_contact": "2026-06-10"}]
    changes = H.repair_followups(contacts, TODAY)
    assert {"id": "CON-1", "field": "next_follow_up",
            "before": None, "after": "2026-06-17"} in changes
    assert contacts[0]["next_follow_up"] == "2026-06-17"  # +7d default cadence


def test_repair_honors_custom_cadence():
    contacts = [{"id": "CON-1", "status": "active", "last_contact": "2026-06-10",
                 "follow_up_cadence_days": 3}]
    H.repair_followups(contacts, TODAY)
    assert contacts[0]["next_follow_up"] == "2026-06-13"


def test_repair_clamps_future_last_contact():
    contacts = [{"id": "CON-1", "status": "contacted", "last_contact": "2099-01-01"}]
    changes = H.repair_followups(contacts, TODAY)
    assert any(c["field"] == "last_contact" and c["after"] == "2026-06-25" for c in changes)
    assert contacts[0]["last_contact"] == "2026-06-25"


def test_repair_skips_dead_and_prospect():
    contacts = [
        {"id": "CON-1", "status": "dead", "last_contact": "2026-06-10"},
        {"id": "CON-2", "status": "prospect", "last_contact": "2026-06-10"},
    ]
    assert H.repair_followups(contacts, TODAY) == []
    assert "next_follow_up" not in contacts[0]
    assert "next_follow_up" not in contacts[1]  # pre-outreach, no cadence expected


def test_repair_ignores_non_dict_rows():
    contacts = ["junk", {"id": "CON-1", "status": "contacted", "last_contact": "2026-06-10"}]
    changes = H.repair_followups(contacts, TODAY)
    assert len(changes) == 1


# ── find_outreach_issues (pure, no mutation) ──────────────────────────────────
def test_flags_overdue_followup():
    contacts = [{"id": "CON-1", "status": "contacted", "last_contact": "2026-05-01",
                 "next_follow_up": "2026-05-08"}]
    issues = H.find_outreach_issues(contacts, TODAY)
    assert any(i["issue"] == "overdue_followup" and i["id"] == "CON-1" for i in issues)


def test_flags_phantom_engaged():
    contacts = [{"id": "CON-1", "status": "active"}]  # engaged but no last_contact/body
    issues = H.find_outreach_issues(contacts, TODAY)
    assert any(i["issue"] == "phantom_engaged" for i in issues)


def test_flags_missing_followup():
    contacts = [{"id": "CON-1", "status": "replied", "last_contact": "2026-06-20"}]
    issues = H.find_outreach_issues(contacts, TODAY)
    assert any(i["issue"] == "missing_followup" for i in issues)


def test_flags_future_last_contact():
    contacts = [{"id": "CON-1", "status": "contacted", "last_contact": "2099-01-01"}]
    issues = H.find_outreach_issues(contacts, TODAY)
    assert any(i["issue"] == "future_last_contact" for i in issues)


def test_no_issues_for_dead_or_healthy():
    contacts = [
        {"id": "CON-1", "status": "dead", "last_contact": "2020-01-01"},
        {"id": "CON-2", "status": "contacted", "last_contact": "2026-06-20",
         "next_follow_up": "2026-06-27"},
    ]
    assert H.find_outreach_issues(contacts, TODAY) == []


# ── contacts_missing_linkedin ─────────────────────────────────────────────────
def test_missing_linkedin_filters_dead_and_present():
    contacts = [
        {"id": "CON-1", "status": "prospect"},                          # missing -> yes
        {"id": "CON-2", "status": "active", "linkedin_url": "https://x"},  # present -> no
        {"id": "CON-3", "status": "dead"},                              # dead -> no
    ]
    out = H.contacts_missing_linkedin(contacts)
    assert [c["id"] for c in out] == ["CON-1"]


# ── source_linkedin with an injected stub runner (NO shell-out) ───────────────
def _runner_returning(payload):
    import json
    return lambda prompt: json.dumps(payload)


def test_source_linkedin_accepts_high_confidence_company_matched():
    contact = {"name": "Priya Raman", "company": "Meridian Robotics", "role": "EM"}
    runner = _runner_returning({
        "linkedin_url": "https://www.linkedin.com/in/priya-raman-example",
        "confidence": "high",
        "evidence": "Profile shows Priya Raman, Engineering Manager at Meridian Robotics.",
    })
    out = H.source_linkedin(contact, runner=runner)
    assert out and out["linkedin_url"].endswith("priya-raman-example")


def test_source_linkedin_rejects_low_confidence():
    contact = {"name": "Priya Raman", "company": "Meridian Robotics"}
    runner = _runner_returning({
        "linkedin_url": "https://www.linkedin.com/in/someone",
        "confidence": "medium",
        "evidence": "Meridian Robotics maybe.",
    })
    assert H.source_linkedin(contact, runner=runner) is None


def test_source_linkedin_rejects_when_company_absent_from_evidence():
    # wrong-person guard: company token must appear in the evidence
    contact = {"name": "Priya Raman", "company": "Meridian Robotics"}
    runner = _runner_returning({
        "linkedin_url": "https://www.linkedin.com/in/priya-raman-other",
        "confidence": "high",
        "evidence": "Profile shows a Priya Raman at SomeOtherCo.",
    })
    assert H.source_linkedin(contact, runner=runner) is None


def test_source_linkedin_rejects_non_profile_url():
    contact = {"name": "Priya Raman", "company": "Meridian Robotics"}
    runner = _runner_returning({
        "linkedin_url": "https://www.linkedin.com/company/meridian",
        "confidence": "high",
        "evidence": "Meridian Robotics company page.",
    })
    assert H.source_linkedin(contact, runner=runner) is None


def test_source_linkedin_handles_fenced_json():
    contact = {"name": "Dana Okonkwo", "company": "Northwind Health"}
    runner = lambda p: ("```json\n"
                        '{"linkedin_url": "https://www.linkedin.com/in/dana-okonkwo-example",'
                        ' "confidence": "high",'
                        ' "evidence": "Dana Okonkwo, Staff Engineer at Northwind Health."}\n```')
    out = H.source_linkedin(contact, runner=runner)
    assert out and "dana-okonkwo-example" in out["linkedin_url"]


def test_source_linkedin_none_when_no_runner_or_no_name():
    assert H.source_linkedin({"name": "X", "company": "Y"}, runner=None) is None
    assert H.source_linkedin({"name": "", "company": "Y"},
                             runner=_runner_returning({})) is None


def test_source_linkedin_survives_runner_garbage():
    contact = {"name": "Priya Raman", "company": "Meridian Robotics"}
    assert H.source_linkedin(contact, runner=lambda p: "not json at all") is None
