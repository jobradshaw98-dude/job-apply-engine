"""Extract CUSTOM application questions (free-text essays + short-text) that the
standard field map didn't cover, so they can be answer-drafted. Y/N and dropdown
custom questions are intentionally left for Sam (quick taps + role-fit judgment)."""
import re
from dataclasses import dataclass, field
from typing import List
from .completeness import label_for, _is_required
from .field_map import map_field
from .work_auth import classify_work_auth, WorkAuthDecision


# Protected-class self-ID (EEO) questions are NEVER auto-answered — left for Sam,
# exactly like work-auth. Live Lever keys them by name="eeo[...]"; we also match on the
# label so a demographic question without the eeo[ name prefix is still caught.
_EEO_LABEL = re.compile(
    r"\b(veteran|disabilit(?:y|ies)|gender|race|ethnicity|hispanic|latino|"
    r"protected|self[- ]?identif)\w*",
    re.IGNORECASE,
)


def _is_eeo(name: str, label: str) -> bool:
    """True for EEO/demographic self-ID widgets we must skip (protected class)."""
    if (name or "").strip().lower().startswith("eeo["):
        return True
    return bool(_EEO_LABEL.search(label or ""))


@dataclass
class Question:
    label: str
    kind: str            # essay (textarea) | short_text (text input)
    selector: str = ""


@dataclass
class SelectQuestion:
    label: str
    options: List[str]   # visible option texts (placeholder/empty option dropped)
    selector: str = ""   # CSS selector for the <select>


@dataclass
class CheckboxGroup:
    label: str
    options: List[str]   # visible label text per checkbox, in DOM order
    selectors: List[str] = field(default_factory=list)  # one selector per checkbox


@dataclass
class ReactSelectQuestion:
    label: str
    options: List[str]   # visible option texts read by briefly opening the control
    selector: str = ""   # the react-select inner input id ("question_<n>"), "" if none


def extract_questions(page, limit: int = 15) -> List[Question]:
    out: List[Question] = []
    sel = "textarea, input[type='text'], input[type='url'], input:not([type])"
    for el in page.query_selector_all(sel):
        try:
            if not el.is_visible():
                continue
            if (el.input_value() or "").strip():
                continue  # already filled
        except Exception:
            continue
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        # Required questions are always captured. OPTIONAL questions: still capture TEXTAREAS
        # (essays) — an unanswered behavioral/experience prompt is a weak submit even when the
        # form marks it optional (Cresta FDE had two optional "tell us about a time…" essays the
        # engine silently skipped, 2026-06-10). Optional non-textarea inputs (website, etc.) are
        # skipped here; map_field below also drops standard fields. Optional essays get DRAFTED but
        # never BLOCK ready_to_submit (completeness only gates on required), so this is purely
        # additive: more answers drafted, no new blockers.
        if tag != "textarea" and not _is_required(page, el):
            continue
        # skip inputs that are really the inner search box of a Yes/No dropdown or
        # React-Select widget — those are not free-text questions (handled elsewhere).
        try:
            if el.evaluate("e => !!e.closest('.select__control, [class*=\"select__\"], "
                           "[class*=\"_yesno\"], [role=\"combobox\"]')"):
                continue
        except Exception:
            pass
        name = el.get_attribute("name") or ""
        label = label_for(page, el)
        # label_for returns the raw name= (or a generic "Type your response") for id-less
        # Lever "cards" textareas — recover the real question from the container's
        # .application-label so the classifiers + the drafter see the human text.
        if not label or label == "field" or label == name or label.lower() == "type your response":
            recovered = _name_group_label(page, el)
            if recovered and recovered != "field":
                label = recovered
        if not label or label == "field" or label == name:
            continue
        # work-auth questions are handled by the work-auth guard — never draft those
        if classify_work_auth(label) != WorkAuthDecision.UNRELATED:
            continue
        # EEO / demographic self-ID — never auto-answered, left for Sam
        if _is_eeo(name, label):
            continue
        # standard fields (city/linkedin/etc.) are handled by fill_remaining — skip them;
        # here we only want genuinely custom questions.
        if map_field(label, name, el.get_attribute("placeholder") or ""):
            continue
        # Prefer #id; live Lever cards are id-less (keyed by name="cards[<uuid>][fieldN]").
        eid = el.get_attribute("id")
        if eid:
            selector = f'[id="{eid}"]'
        elif name:
            selector = f'[name="{name}"]'
        else:
            continue
        out.append(Question(label=label,
                            kind="essay" if tag == "textarea" else "short_text",
                            selector=selector))
        if len(out) >= limit:
            break
    return out


def _custom_select_label(page, el):
    """Return the label for a required CUSTOM <select> we should answer, or None if this
    select is one we must NOT touch: not visible, not required, already chosen, a work-auth
    question (guard owns it), or a standard-mapped field (city/country/etc.)."""
    try:
        if not el.is_visible():
            return None
        if (el.input_value() or "").strip():
            return None  # already chosen
    except Exception:
        return None
    if not _is_required(page, el):
        return None
    label = label_for(page, el)
    # label_for returns the raw name= for id-less selects (no for= label, no aria-label).
    # Live Lever "cards" selects have no id; recover the real label from the container's
    # .application-label so work-auth/EEO classification sees the human question text.
    name = el.get_attribute("name") or ""
    if not label or label == "field" or label == name:
        recovered = _name_group_label(page, el)
        if recovered and recovered != "field":
            label = recovered
    if not label or label == "field" or label == name:
        return None
    if classify_work_auth(label) != WorkAuthDecision.UNRELATED:
        return None  # work-auth guard owns this one
    if _is_eeo(el.get_attribute("name") or "", label):
        return None  # protected-class self-ID — left for Sam, never auto-answered
    if map_field(label, el.get_attribute("name") or "",
                 el.get_attribute("placeholder") or ""):
        return None  # standard field (country/state/...) handled elsewhere
    return label


def extract_select_questions(page, limit: int = 15) -> List[SelectQuestion]:
    """Required custom <select> dropdowns that are NOT work-auth and NOT standard-mapped.
    Reads each option's visible text (dropping the empty/placeholder option) so the picker
    can choose one grounded option. Empty-option dropdowns (no real choices) are skipped."""
    out: List[SelectQuestion] = []
    for el in page.query_selector_all("select"):
        label = _custom_select_label(page, el)
        if not label:
            continue
        options: List[str] = []
        for opt in el.query_selector_all("option"):
            val = (opt.get_attribute("value") or "").strip()
            txt = (opt.inner_text() or "").strip()
            if not val and not txt:
                continue
            if not val:
                continue  # placeholder ("Select"/"--") — has text but no value
            if txt:
                options.append(txt)
        if not options:
            continue
        # Prefer #id; live Lever "cards" selects have NO id, so fall back to name= (they
        # are keyed by name="cards[<uuid>][field0]"). Skip only when neither is addressable.
        eid = el.get_attribute("id")
        if eid:
            selector = f'[id="{eid}"]'
        else:
            name = el.get_attribute("name")
            if not name:
                continue
            selector = f'select[name="{name}"]'
        out.append(SelectQuestion(label=label, options=options, selector=selector))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Modern React-select questions (job-boards.greenhouse.io renders screening Yes/No
# AND searchable dropdowns as .select__control, NEVER native <select> — so
# extract_select_questions finds nothing and the screening questions were silently
# missed). These are the CUSTOM ones only: work-auth (the guard owns it), EEO, and
# standard-mapped Country/State are all excluded.
# ---------------------------------------------------------------------------

def _react_select_label(ctrl) -> str:
    """Label text for a .select__control. Prefers `label[for="<combobox input id>"]` (the
    gold standard on Greenhouse — correct even when the control sits in a multi-field
    container whose first <label> belongs to another field, e.g. Country was mislabeled
    'First Name' by a greedy wrapper walk). Falls back to the immediate .field-wrapper label.
    Drops the required-marker '*'. Returns '' if none."""
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


def _read_react_options(page, ctrl, cap: int = 80) -> List[str]:
    """Open a .select__control, collect its visible .select__option texts, then close it.
    Lets the grounded picker enumerate a custom react-select's choices. Best-effort and
    non-mutating beyond open/close (Escape). A cascade not yet populated shows a
    .select__menu-notice ('No options') and yields [] — the caller then leaves it."""
    opts: List[str] = []
    try:
        ctrl.scroll_into_view_if_needed()
        ctrl.click()
        page.wait_for_timeout(250)
    except Exception:
        return opts
    try:
        page.wait_for_selector(".select__option", timeout=1500)
    except Exception:
        pass
    try:
        for o in page.query_selector_all(".select__option"):
            t = (o.inner_text() or "").strip()
            if t and t not in opts:
                opts.append(t)
            if len(opts) >= cap:
                break
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    except Exception:
        pass
    return opts


def extract_react_select_questions(page, limit: int = 15) -> List[ReactSelectQuestion]:
    """Required CUSTOM React-select questions on a modern board. Excludes work-auth (the
    guard owns those), EEO/demographic self-ID, standard-mapped fields (Country/State/city/
    ...), and any control already showing a chosen value (.select__single-value). Options are
    read by briefly opening each control so the grounded picker has a fixed option set."""
    out: List[ReactSelectQuestion] = []
    for ctrl in page.query_selector_all(".select__control"):
        try:
            if not ctrl.is_visible():
                continue
            # already chosen? a selected react-select shows a single/multi value chip.
            if ctrl.query_selector(".select__single-value, .select__multi-value"):
                continue
        except Exception:
            continue
        label = _react_select_label(ctrl)
        if not label:
            continue
        # work-auth guard owns these
        if classify_work_auth(label) != WorkAuthDecision.UNRELATED:
            continue
        # protected-class self-ID — never auto-answered
        if _is_eeo("", label):
            continue
        # standard fields (country/state/city/...) are driven by the adapter, not drafted
        if map_field(label, "", ""):
            continue
        qid = ""
        try:
            inp = ctrl.query_selector("input")
            if inp:
                qid = inp.get_attribute("id") or ""
        except Exception:
            qid = ""
        options = _read_react_options(page, ctrl)
        if not options:
            continue  # nothing enumerable (e.g. an unpopulated cascade) — leave it
        out.append(ReactSelectQuestion(label=label, options=options, selector=qid))
        if len(out) >= limit:
            break
    return out


def _checkbox_group_label(page, fieldset) -> str:
    """Label a checkbox-group by its <legend>, else aria-label, else 'field'."""
    leg = fieldset.query_selector("legend")
    if leg:
        t = (leg.inner_text() or "").strip()
        if t:
            return t.replace("*", "").strip()
    v = fieldset.get_attribute("aria-label")
    if v and v.strip():
        return v.strip()
    return "field"


def extract_checkbox_groups(page, limit: int = 10) -> List[CheckboxGroup]:
    """Find "check all that apply" checkbox-groups: a <fieldset> holding 2+ checkboxes that
    share a label/legend. Required + UNRELATED-work-auth only. Each box's adjacent label text
    is the option; one selector per box (in DOM order) so the caller can check the chosen subset."""
    out: List[CheckboxGroup] = []
    for fs in page.query_selector_all("fieldset"):
        try:
            if not fs.is_visible():
                continue
        except Exception:
            continue
        boxes = [b for b in fs.query_selector_all("input[type='checkbox']")]
        boxes = [b for b in boxes if _safe_visible(b)]
        if len(boxes) < 2:
            continue  # a lone checkbox is not a "select all" group
        if not _is_required(page, fs):
            continue
        label = _checkbox_group_label(page, fs)
        if not label or label == "field":
            continue
        if classify_work_auth(label) != WorkAuthDecision.UNRELATED:
            continue  # work-auth guard owns it
        options: List[str] = []
        selectors: List[str] = []
        for i, b in enumerate(boxes):
            txt = _checkbox_option_text(page, b)
            if not txt:
                continue
            sel = _checkbox_selector(b, fs, i)
            if not sel:
                continue
            options.append(txt)
            selectors.append(sel)
        if len(options) < 2:
            continue
        out.append(CheckboxGroup(label=label, options=options, selectors=selectors))
        if len(out) >= limit:
            break

    # Name-based grouping for ATSs (live Lever) that DON'T wrap a check-all group in a
    # <fieldset>: 2+ checkboxes sharing a name= are one "check all that apply" question.
    # Added as a second source; deduped against the fieldset groups above by label.
    seen_labels = {g.label for g in out}
    for g in _name_grouped_checkboxes(page):
        if len(out) >= limit:
            break
        if g.label in seen_labels:
            continue  # a fieldset already emitted this group
        seen_labels.add(g.label)
        out.append(g)
    return out


def _name_grouped_checkboxes(page) -> List[CheckboxGroup]:
    """Group all visible checkboxes by their shared name= (no <fieldset> needed). A name
    with 2+ boxes is a check-all group. Required + non-EEO + UNRELATED-work-auth only;
    option text = the box's value (Lever's value IS the display text), selectors use the
    value double-quoted so spaces/parens are valid."""
    groups: "dict[str, list]" = {}
    order: List[str] = []
    for b in page.query_selector_all("input[type='checkbox']"):
        if not _safe_visible(b):
            continue
        name = b.get_attribute("name")
        if not name:
            continue
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(b)

    out: List[CheckboxGroup] = []
    for name in order:
        boxes = groups[name]
        if len(boxes) < 2:
            continue  # a lone checkbox is not a "check all" group
        label = _name_group_label(page, boxes[0])
        if not label or label == "field":
            continue  # never answer an unlabeled group
        if classify_work_auth(label) != WorkAuthDecision.UNRELATED:
            continue  # work-auth guard owns it
        if _is_eeo(name, label):
            continue  # protected-class self-ID — left for Sam
        if not _name_group_required(page, boxes[0]):
            continue
        options: List[str] = []
        selectors: List[str] = []
        for b in boxes:
            txt = _checkbox_option_text(page, b)
            val = b.get_attribute("value")
            if not txt or val is None:
                continue
            options.append(txt)
            selectors.append(f'input[type="checkbox"][name="{name}"][value="{val}"]')
        if len(options) < 2:
            continue
        out.append(CheckboxGroup(label=label, options=options, selectors=selectors))
    return out


def _name_group_container(box):
    """The nearest .application-question (else .application-field) ancestor that holds the
    group's label — Lever's container chain. Returns an element handle or None."""
    try:
        return box.evaluate_handle(
            "e => e.closest('.application-question') || e.closest('.application-field')"
        ).as_element()
    except Exception:
        return None


def _name_group_label(page, box) -> str:
    """Group label from the container's .application-label / legend / label text, else the
    box's own aria-label, else 'field'."""
    container = _name_group_container(box)
    if container is not None:
        for sel in (".application-label", "legend", "label"):
            lab = container.query_selector(sel)
            if lab:
                t = (lab.inner_text() or "").strip()
                if t:
                    return t.replace("*", "").strip()
    v = box.get_attribute("aria-label")
    if v and v.strip():
        return v.strip()
    return "field"


def _name_group_required(page, box) -> bool:
    """Required if the box (or its labelling container) signals required. Live Lever marks
    the group required via a '*' in the .application-label, not on the <input>."""
    if _is_required(page, box):
        return True
    container = _name_group_container(box)
    if container is not None:
        lab = container.query_selector(".application-label")
        if lab and "*" in (lab.inner_text() or ""):
            return True
        if (container.get_attribute("class") or "").lower().find("required") != -1:
            return True
    return False


def _safe_visible(el) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False


def _checkbox_option_text(page, box) -> str:
    """The visible label for a single checkbox: its <label for=id>, else the text of an
    enclosing <label>, else its value attribute."""
    bid = box.get_attribute("id")
    if bid:
        lab = page.query_selector(f"label[for='{bid}']")
        if lab:
            t = (lab.inner_text() or "").strip()
            if t:
                return t
    try:
        t = box.evaluate(
            "e => { const l = e.closest('label'); return l ? l.innerText.trim() : ''; }")
        if t:
            return t.strip()
    except Exception:
        pass
    return (box.get_attribute("value") or "").strip()


def _checkbox_selector(box, fieldset, index: int) -> str:
    """A stable selector to a single checkbox. Prefer #id; else an nth-of-type path scoped
    to the fieldset's id; else "" (skip — we never check a box we can't address)."""
    bid = box.get_attribute("id")
    if bid:
        return f'[id="{bid}"]'
    fsid = fieldset.get_attribute("id")
    if fsid:
        return f'[id="{fsid}"] input[type=\'checkbox\']:nth-of-type({index + 1})'
    name = box.get_attribute("name")
    val = box.get_attribute("value")
    if name and val:
        return f"input[type='checkbox'][name='{name}'][value='{val}']"
    return ""
