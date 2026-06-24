"""Completeness + attachment checks so 'ready_to_submit' can never hide a blank
required field. Answers Sam's rule: ensure all fields are filled — or report
exactly which required ones still need him.

Covers native fillable controls (text/textarea/select/checkbox/radio) + resume file
attachment. Custom JS widgets (React-Select, Yes/No buttons) are answered by the
adapter; their underlying required input is included here when the DOM exposes one."""
from typing import List


def label_for(page, el) -> str:
    eid = el.get_attribute("id")
    if eid:
        lab = page.query_selector(f"label[for='{eid}']")
        if lab:
            t = (lab.inner_text() or "").strip()
            if t:
                return t.replace("*", "").strip()
    for attr in ("aria-label", "placeholder", "name"):
        v = el.get_attribute(attr)
        if v and v.strip():
            return v.strip()
    return "field"


def resume_attached(page, resume_selector: str) -> bool:
    if not resume_selector:
        return True
    el = page.query_selector(resume_selector)
    if not el:
        return False
    try:
        return bool(el.evaluate("e => !!(e.files && e.files.length > 0)"))
    except Exception:
        return False


def _is_required(page, el) -> bool:
    if el.get_attribute("required") is not None:
        return True
    if (el.get_attribute("aria-required") or "").lower() == "true":
        return True
    cls = (el.get_attribute("class") or "").lower()
    if "required" in cls:
        return True
    eid = el.get_attribute("id")
    if eid:
        lab = page.query_selector(f"label[for='{eid}']")
        if lab:
            if "*" in (lab.inner_text() or ""):
                return True
            if "required" in (lab.get_attribute("class") or "").lower():
                return True
    return False


def _is_empty(el) -> bool:
    t = (el.get_attribute("type") or "").lower()
    tag = el.evaluate("e => e.tagName.toLowerCase()")
    try:
        if t == "file":
            return not el.evaluate("e => !!(e.files && e.files.length > 0)")
        if t in ("checkbox", "radio"):
            return not el.is_checked()
        if tag == "select":
            return not (el.input_value() or "").strip()
        return not (el.input_value() or "").strip()
    except Exception:
        return False


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def drop_answered(missing: List[str], answered_questions: List[str]) -> List[str]:
    """Remove from `missing` any field whose label matches a work-auth question the
    adapter already answered. Needed because React-Select / Yes-No-button widgets store
    their value in JS state the DOM scanner can't read, so they look 'empty' even when
    correctly answered (verified live on Greenhouse 2026-05-31)."""
    ans = [_norm(q) for q in answered_questions]
    return [m for m in missing
            if not any(a and (_norm(m) in a or a in _norm(m)) for a in ans)]


def yesno_button_groups(page) -> List[str]:
    """Labels of REQUIRED Yes/No (2-button) groups — Ashby-style custom widgets with NO
    native input, so `unfilled_required` (which scans input/textarea/select) can't see them.
    The orchestrator adds these to the missing set; work-auth groups the adapter already
    answered are removed by `drop_answered`, so any OTHER required Y/N (e.g. "willing to
    relocate?") correctly forces needs_input instead of a false ready_to_submit. Optional
    groups (no required marker) are ignored so they never block."""
    out: List[str] = []
    for lbl in page.query_selector_all("label"):
        try:
            if not lbl.is_visible():
                continue
        except Exception:
            continue
        text = (lbl.inner_text() or "").strip()
        if not text:
            continue
        if not (("*" in text) or _is_required(page, lbl)):
            continue
        block = lbl.evaluate_handle("e => e.closest('div, fieldset, li')")
        el = block.as_element() if block else None
        if not el:
            continue
        btn_txts = {(b.inner_text() or "").strip().lower()
                    for b in el.query_selector_all("button")}
        if {"yes", "no"} <= btn_txts:
            clean = text.replace("*", "").strip()
            if clean and clean not in out:
                out.append(clean)
    return out


def _react_wrapper_label(ctrl) -> str:
    """Label text for a .select__control. Prefers `label[for="<combobox input id>"]` — the
    gold standard on Greenhouse (correct even when the control sits in a multi-field container
    whose first <label> belongs to another field, e.g. Country mislabeled 'First Name' by a
    greedy wrapper walk). Falls back to the immediate .field-wrapper label. '' if none. Kept
    local (not imported from questions) to avoid the questions<->completeness import cycle."""
    try:
        t = ctrl.evaluate(
            "e => {"
            " const inp = e.querySelector('input');"
            " const id = inp && inp.getAttribute('id');"
            " if (id) { const l = document.querySelector('label[for=\"'+id+'\"]');"
            "   if (l && l.innerText.trim()) return l.innerText.trim(); }"
            " const w = e.closest('.field-wrapper');"
            " const wl = w && w.querySelector('label');"
            " return wl ? wl.innerText.trim() : ''; }")
        if t:
            return " ".join(t.replace("*", " ").split())
    except Exception:
        pass
    return ""


def _react_select_required(ctrl) -> bool:
    """A react-select is required if its field wrapper's <label> carries a '*' or a required
    class, the wrapper itself is marked required, or the control has aria-required=true."""
    try:
        if (ctrl.get_attribute("aria-required") or "").lower() == "true":
            return True
        h = ctrl.evaluate_handle(
            "e => e.closest('.field-wrapper, [class*=field], form > div')")
        el = h.as_element() if h else None
        if el is not None:
            lab = el.query_selector("label")
            if lab:
                if "*" in (lab.inner_text() or ""):
                    return True
                if "required" in (lab.get_attribute("class") or "").lower():
                    return True
            if "required" in (el.get_attribute("class") or "").lower():
                return True
    except Exception:
        pass
    return False


def react_select_unfilled(page) -> List[str]:
    """Labels of REQUIRED React-select widgets (.select__control) with NO chosen value.

    React-select keeps its value in JS state shown as .select__single-value; the inner input
    is always blank, so `unfilled_required` can't judge it (and excludes it). A control with a
    single/multi value chip is satisfied. Work-auth react-selects the adapter already answered
    are removed downstream by `drop_answered` against the answered-question labels. Without this
    a blank required Country/State/screening-select would slip through as a false ready_to_submit
    on modern boards (live-verified gap on job-boards.greenhouse.io 2026-06-07)."""
    out: List[str] = []
    try:
        controls = page.query_selector_all(".select__control")
    except Exception:
        return out
    for ctrl in controls:
        try:
            if not ctrl.is_visible():
                continue
            if ctrl.query_selector(".select__single-value, .select__multi-value"):
                continue  # already chosen
        except Exception:
            continue
        if not _react_select_required(ctrl):
            continue
        label = _react_wrapper_label(ctrl)
        if label and label != "field" and label not in out:
            out.append(label)
    return out


def _radio_group_label(page, radio) -> str:
    """Question label for a radio group — recovered from the card's `.application-label`
    (Lever) via the questions module, not a `<label for=>`. Lazy import avoids a cycle
    (questions imports this module). Returns '' (not the 'field' sentinel)."""
    try:
        from .questions import _name_group_label
        lab = _name_group_label(page, radio)
        return "" if lab == "field" else lab
    except Exception:
        return ""


def unfilled_required(page) -> List[str]:
    """Labels of visible, required, still-empty fields after filling.

    Radio groups are handled by NAME (satisfied when ANY radio in the group is checked) and
    reported by their recovered QUESTION label, not the raw field name. A naive per-input scan
    flags every unchecked sibling radio of an answered group as 'missing' — which is exactly
    why a correctly-answered Lever work-auth radio still showed up under 'needs Sam'."""
    missing: List[str] = []
    radio_names: List[str] = []
    for el in page.query_selector_all("input, textarea, select"):
        t = (el.get_attribute("type") or "").lower()
        if t in ("hidden", "submit", "button", "reset"):
            continue
        if t == "radio":
            nm = el.get_attribute("name")
            if nm and nm not in radio_names:
                radio_names.append(nm)
            continue  # radio groups handled below, by name
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue
        # react-select internals (the combobox input AND Greenhouse's hidden 'requiredInput'
        # proxy that sits in .select__container, a SIBLING of .select__control) are blank by
        # design — value lives in JS state, judged by react_select_unfilled. Match any
        # [class*="select__"] ancestor: .select__control misses the proxy and it leaked as a
        # phantom unlabeled "field" blocker (live-verified on Oura Greenhouse 2026-06-08).
        try:
            if el.evaluate("e => !!e.closest('[class*=\"select__\"]')"):
                continue
        except Exception:
            pass
        if not _is_required(page, el):
            continue
        if _is_empty(el):
            lbl = label_for(page, el)
            if lbl and lbl not in missing:
                missing.append(lbl)
    # radio groups: one entry per required, unanswered group, keyed by question label
    for nm in radio_names:
        radios = [r for r in page.query_selector_all(f'input[type="radio"][name="{nm}"]')
                  if _safe_visible(r)]
        if not radios:
            continue
        if not any(_is_required(page, r) for r in radios):
            continue
        if any(r.is_checked() for r in radios):
            continue  # group satisfied by one selected option
        lbl = _radio_group_label(page, radios[0]) or nm
        if lbl not in missing:
            missing.append(lbl)
    return missing


def _safe_visible(el) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False
