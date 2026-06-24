"""WorkdayAdapter custom-question resolution: a grounded pick is auto-selected; a
DECLINE / gate-block / failed-select / no-resolver case escalates to the user (never a
guess). Browser-free — W.read_options / W.button_select are stubbed."""
import apply_engine.wd_widgets as W
from apply_engine.adapters.workday import WorkdayAdapter
from apply_engine.choice_gen import Choice


def test_grounded_option_is_auto_selected(monkeypatch):
    monkeypatch.setattr(W, "read_options", lambda page, fid: ["0-2 years", "5+ years"])
    picked = {}
    monkeypatch.setattr(W, "button_select",
                        lambda page, fid, val: (picked.update(val=val), True)[1])
    a = WorkdayAdapter()
    a._choose = lambda q, opts: Choice(q, opts, value="5+ years", status="answered")
    result = {"escalations": []}
    a._resolve_custom_question(None, "primaryQuestionnaire--exp", "Years of experience?", result)
    assert picked["val"] == "5+ years"
    assert result["escalations"] == []


def test_decline_escalates_and_never_selects(monkeypatch):
    monkeypatch.setattr(W, "read_options", lambda page, fid: ["Very", "Somewhat", "Not at all"])

    def _must_not_select(page, fid, val):
        raise AssertionError("must not select an option on DECLINE")
    monkeypatch.setattr(W, "button_select", _must_not_select)
    a = WorkdayAdapter()
    a._choose = lambda q, opts: Choice(q, opts, status="declined",
                                       reason="not supported by facts")
    result = {"escalations": []}
    a._resolve_custom_question(None, "primaryQuestionnaire--fam",
                               "How familiar are you with Illumina?", result)
    assert len(result["escalations"]) == 1
    assert "declined" in result["escalations"][0]["reason"]


def test_failed_select_escalates(monkeypatch):
    monkeypatch.setattr(W, "read_options", lambda page, fid: ["A", "B"])
    monkeypatch.setattr(W, "button_select", lambda page, fid, val: False)  # click didn't take
    a = WorkdayAdapter()
    a._choose = lambda q, opts: Choice(q, opts, value="A", status="answered")
    result = {"escalations": []}
    a._resolve_custom_question(None, "primaryQuestionnaire--x", "Q?", result)
    assert len(result["escalations"]) == 1


class _StubPage:
    """Minimal page that lets stage_application build its resolver, then aborts the DOM
    walk so the rest doesn't need a browser. wait_for_timeout raises to bail out of
    _apply_entry; the error is caught by stage_application's own try/except."""
    def wait_for_selector(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        raise RuntimeError("stop walk early (no browser)")

    def query_selector(self, *a, **k):
        return None


def test_stage_application_runs_the_real_make_resolver_call():
    # Regression for the autoflake-stripped import: make_resolver is called at the top of
    # stage_application BEFORE the try-block, so a missing import would NameError here. The
    # fakes in the other tests bypass this; this one exercises the real method.
    a = WorkdayAdapter()
    res = a.stage_application(_StubPage(), object(), {}, {"id": "X"})
    assert isinstance(res, dict)
    assert a._choose is None  # no hooks -> escalate-everything default


def test_stage_application_builds_a_resolver_when_hooks_given():
    a = WorkdayAdapter()
    a.stage_application(_StubPage(), object(), {}, {"id": "X"},
                        answer_fn=lambda p: "x", audit_fn=lambda t: [], facts="F")
    assert callable(a._choose)


def test_no_resolver_escalates_blank():
    a = WorkdayAdapter()
    a._choose = None
    result = {"escalations": []}
    a._resolve_custom_question(None, "primaryQuestionnaire--x", "Q?", result)
    assert len(result["escalations"]) == 1
    assert "left blank" in result["escalations"][0]["reason"]
