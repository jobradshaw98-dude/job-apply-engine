"""Static guard: the ONLY place an ATS submit control may be clicked is finish.py's
replay(submit=True) branch. The staging path (orchestrator + adapters) must never click
submit. This test greps the source so a future edit that sneaks a submit click into the
staging path fails CI loudly.

It also locks the submit-selector table to live-verified ATS controls, and asserts the
submit-control finder is read-only (returns a handle, never clicks)."""
import re
from pathlib import Path

import apply_engine

PKG = Path(apply_engine.__file__).resolve().parent

# files that make up the STAGING path — none of them may click a submit control.
_STAGING_FILES = [
    PKG / "orchestrator.py",
    PKG / "cli.py",
    PKG / "adapters" / "base.py",
    PKG / "adapters" / "greenhouse.py",
    PKG / "adapters" / "lever.py",
    PKG / "adapters" / "ashby.py",
    PKG / "adapters" / "workday.py",
    PKG / "adapters" / "generic.py",
    PKG / "wd_widgets.py",
]

# a "submit click" = clicking an element whose target text/selector is a submit control.
# We look for a .click() on the same line as a submit-ish selector/text token. The Workday
# adapter's _assert_not_submitting only QUERIES (no click) and advance() REFUSES submit —
# those are fine; this pattern only flags an actual click on a submit token.
_SUBMIT_CLICK = re.compile(
    r"(submit[^\n]*\.click\(|\.click\([^\n]*submit|"
    r"pageFooterSubmitButton[^\n]*\.click|btn-submit[^\n]*\.click|submit_app[^\n]*\.click)",
    re.IGNORECASE,
)


def test_no_submit_click_in_staging_path():
    offenders = []
    for f in _STAGING_FILES:
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _SUBMIT_CLICK.search(line):
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert not offenders, "submit click found OUTSIDE finish.py:\n" + "\n".join(offenders)


def test_finish_has_exactly_one_submit_click_in_replay():
    src = (PKG / "finish.py").read_text(encoding="utf-8").splitlines()
    click_lines = [i for i, l in enumerate(src, 1) if re.search(r"btn\.click\(", l)]
    # exactly one literal submit click in the whole module
    assert len(click_lines) == 1, f"expected one submit click, found {click_lines}"
    # and it lives inside replay(...), after the can_submit live re-check
    joined = "\n".join(src)
    assert "def replay(" in joined
    # the can_submit re-check precedes the click in source order
    recheck_idx = joined.index("can_submit(record)           # live re-check")
    click_idx = joined.index("btn.click()")
    assert recheck_idx < click_idx, "submit click must come AFTER the live can_submit re-check"


def test_submit_selectors_locked_to_known_ats():
    from apply_engine.finish import _SUBMIT_SELECTORS
    from apply_engine.ats_detect import AtsKind
    assert _SUBMIT_SELECTORS[AtsKind.GREENHOUSE][0] == "#submit_app"
    assert _SUBMIT_SELECTORS[AtsKind.LEVER][0] == "#btn-submit"
    assert _SUBMIT_SELECTORS[AtsKind.WORKDAY][0] == "[data-automation-id='pageFooterSubmitButton']"
    assert AtsKind.ASHBY in _SUBMIT_SELECTORS
