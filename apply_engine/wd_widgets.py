# -*- coding: utf-8 -*-
"""Reusable, page-level Workday widget helpers.

Ported faithfully from the proven prototype (`aria/tmp/workday_walk.py`) which drove
a live Illumina Workday application end-to-end to the Review page. No global state —
every function takes `page`. The fixes preserved here are exactly WHY the live flow
works; do not "simplify" them:

  * Open dropdowns / click Save&Continue with an ACTIONABLE click (Playwright locator
    `.click()` or a real `page.mouse.click(x,y)` at the element center), NEVER
    `force=True` — force-clicks don't fire Workday's React handlers.
  * `pick_option` EXCLUDES option-like nodes inside
    `[data-automation-id='selectedItemList']` — those are selected-value chips that
    masquerade as options (the stale "+1" country-code bug).
  * Option matching prefers exact / word-start over substring, so "No" does NOT match
    "now" inside "...sponsorship now or in the future..." (a real, dangerous false
    positive). The matcher is the pure function `score_option_match`.
  * `pick_option` scrolls the open `activeListContainer` listbox to load virtualized
    options (e.g. Country Phone Code).
  * `advance` polls `active_step` up to ~15s for the step label to actually change
    (Workday saves async); and REFUSES any button whose text contains "submit".
"""


# ---- pure helpers (unit-testable without a browser) ----
def esc_id(element_id: str) -> str:
    """CSS-escape an id containing `--` so it can be used in a `#id` selector."""
    return "#" + element_id.replace("-", "\\-")


def score_option_match(option_text: str, target: str) -> int:
    """Score how well an option's text matches a target choice. PURE (no browser).

    Returns: 2 = exact or word-start match, 1 = substring fallback, 0 = no match.

    Word-start match is preferred over substring so "No" does NOT match "now" inside
    "Yes, I will need sponsorship now or in the future" — staging that as the answer
    would set the wrong (red-flag) immigration response. This is correctness-critical.
    """
    t = (option_text or "").lower().lstrip()
    c = (target or "").lower()
    if not c:
        return 0
    if t == c:
        return 2
    # starts with the term AND the next char is a word boundary (not another letter)
    if t.startswith(c) and (len(t) == len(c) or not t[len(c)].isalpha()):
        return 2
    # substring fallback, but ONLY at a word boundary on BOTH sides. This rejects the
    # dangerous "No" matching the "no" inside "now" — that "no" starts mid-word and ends
    # against a letter, so it never counts. "Familiar" inside "Extremely familiar" still
    # matches (preceded by a space, ends at string end).
    idx = t.find(c)
    while idx != -1:
        left_ok = (idx == 0) or (not t[idx - 1].isalnum())
        end = idx + len(c)
        right_ok = (end == len(t)) or (not t[end].isalnum())
        if left_ok and right_ok:
            return 1
        idx = t.find(c, idx + 1)
    return 0


# ---- page-level helpers ----
def is_visible(el) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False


def close_popups(page) -> None:
    """Press Escape twice — open Workday dropdowns don't auto-close, so option lists
    bleed together between widgets unless we dismiss them."""
    try:
        page.keyboard.press("Escape"); page.wait_for_timeout(250)
        page.keyboard.press("Escape"); page.wait_for_timeout(250)
    except Exception:
        pass


def wait_options(page, timeout: int = 4500) -> bool:
    try:
        page.wait_for_selector(
            "[data-automation-id='promptOption'], [role='option'], ul[role='listbox'] li",
            timeout=timeout, state="visible")
        return True
    except Exception:
        return False


def active_step(page) -> str:
    a = page.query_selector("[data-automation-id='progressBarActiveStep']")
    return (a.inner_text().strip().replace("\n", " ").lower() if a else "")


def settle(page) -> None:
    """Wait for the step's form content to render (not just the progress label flip):
    wait for the footer / loading panel, then let any spinner clear and content paint."""
    try:
        page.wait_for_selector(
            "[data-automation-id='pageFooterNextButton'], "
            "[data-automation-id='wd-LoadingPanel']", timeout=12000)
    except Exception:
        pass
    for _ in range(8):
        page.wait_for_timeout(700)
        if not page.query_selector("[data-automation-id='wd-LoadingPanel']"):
            break
    page.wait_for_timeout(1200)


def pick_option(page, contains: str, debug: bool = False) -> bool:
    """Click the open listbox/prompt option that best matches `contains`.

    Prefers exact/word-start over substring (see score_option_match). Excludes selected-
    value chips inside [data-automation-id='selectedItemList']. Scrolls the open
    activeListContainer to load virtualized options (long lists like Country Phone Code).
    """
    page.wait_for_timeout(500)

    def gather():
        out = []
        for opt in page.query_selector_all(
                "li[role='option'], [data-automation-id='promptOption'], [role='option']"):
            try:
                if not is_visible(opt):
                    continue
                if opt.evaluate("e => !!e.closest(\"[data-automation-id='selectedItemList']\")"):
                    continue
                out.append((opt, (opt.inner_text() or "").strip()))
            except Exception:
                pass
        return out

    def scroll_list():
        page.evaluate("""() => {
          const c = document.querySelector("[data-automation-id='activeListContainer']")
                 || document.querySelector("[role='listbox']:not([data-automation-id='selectedItemList'])");
          if (c) c.scrollTop = c.scrollTop + Math.max(c.clientHeight * 0.85, 250);
        }""")

    dumped = False
    last_first = None
    for _ in range(14):
        pairs = gather()
        if debug and not dumped:
            print(f"     [pick_option] options: {[t for _, t in pairs][:20]}", flush=True)
            dumped = True
        # prefer the strongest match (exact/word-start) over a weak substring match
        best = None
        for opt, txt in pairs:
            s = score_option_match(txt, contains)
            if s and (best is None or s > best[0]):
                best = (s, opt, txt)
        if best:
            try:
                best[1].click(force=True, timeout=2500)
                page.wait_for_timeout(500)
                return True
            except Exception:
                pass
        # not found in the currently-rendered window -> scroll to load more
        first = pairs[0][1] if pairs else None
        scroll_list()
        page.wait_for_timeout(450)
        after = gather()
        new_first = after[0][1] if after else None
        if new_first == first == last_first:
            break  # scrolling no longer changes the window -> reached the end
        last_first = first
    return False


def button_select(page, button_id: str, option_text: str) -> bool:
    """Open a single-select button dropdown (#button_id) and pick the matching option.

    Opens with an ACTIONABLE click (not force) so Workday's React open handler fires;
    falls back to a real mouse click at the button center. Retries up to 3x because a
    freshly-rendered dropdown can open empty if clicked too soon.
    """
    btn = page.query_selector(esc_id(button_id))
    if not btn:
        return False
    opened = False
    for _ in range(3):
        close_popups(page)
        try:
            btn.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(400)
        try:
            btn.click(timeout=3000)
        except Exception:
            box = btn.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        if wait_options(page, 3500):
            page.wait_for_timeout(300)
            # confirm real (non-chip) options actually rendered
            if any(is_visible(o) and not o.evaluate(
                       "e=>!!e.closest(\"[data-automation-id='selectedItemList']\")")
                   for o in page.query_selector_all(
                       "[role='option'], [data-automation-id='promptOption']")):
                opened = True
                break
        page.wait_for_timeout(700)
    if not opened:
        return False
    sb = page.query_selector("input[data-automation-id='searchBox']")
    if sb:
        sb.fill(option_text[:14])
        wait_options(page, 2500)
    ok = pick_option(page, option_text)
    close_popups(page)
    return ok


def read_options(page, button_id: str) -> list:
    """Open a single-select button dropdown (#button_id), read the visible REAL option
    texts (excluding selected-value chips inside selectedItemList), then close it.

    Used to hand a custom question's offered options to the grounded-choice picker. Opens
    with an actionable click (same proven sequence as button_select); returns [] if the
    dropdown won't open or renders no real options. Always closes the popup before returning
    so the next widget isn't polluted by a left-open list."""
    btn = page.query_selector(esc_id(button_id))
    if not btn:
        return []
    opened = False
    for _ in range(3):
        close_popups(page)
        try:
            btn.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(400)
        try:
            btn.click(timeout=3000)
        except Exception:
            box = btn.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        if wait_options(page, 3500):
            page.wait_for_timeout(300)
            opened = True
            break
        page.wait_for_timeout(700)
    if not opened:
        close_popups(page)
        return []
    texts = []
    for opt in page.query_selector_all(
            "li[role='option'], [data-automation-id='promptOption'], [role='option']"):
        try:
            if not is_visible(opt):
                continue
            if opt.evaluate("e => !!e.closest(\"[data-automation-id='selectedItemList']\")"):
                continue
            t = (opt.inner_text() or "").strip()
            if t and t not in texts:
                texts.append(t)
        except Exception:
            pass
    close_popups(page)
    return texts


def multiselect(page, input_id: str, type_text: str, picks) -> bool:
    """Open a typeahead multiselect (#input_id), optionally type to filter, then click
    through the cascade of option picks (each pick can reveal the next level)."""
    close_popups(page)
    inp = page.query_selector(esc_id(input_id))
    if not inp:
        return False
    try:
        inp.click(force=True, timeout=4000)
    except Exception:
        cont = inp.evaluate_handle("e=>e.closest('[data-automation-id=multiselectInputContainer]')")
        ce = cont.as_element() if cont else None
        if ce:
            ce.click(force=True, timeout=4000)
    page.wait_for_timeout(500)
    if type_text:
        # Workday renders a SEPARATE search box in the open popup; type there (the field's
        # own input does not filter a long virtualized list). Fall back to the field input,
        # then to page.keyboard (auto-focused search) if no explicit box.
        sb = page.query_selector("input[data-automation-id='searchBox']")
        target = sb or inp
        try:
            target.click(force=True)
            try:
                target.press_sequentially(type_text, delay=55)
            except Exception:
                page.keyboard.type(type_text, delay=55)
            page.wait_for_timeout(1100)
        except Exception:
            pass
    results = []
    for contains in picks:
        wait_options(page)
        results.append(pick_option(page, contains))
        page.wait_for_timeout(600)
    close_popups(page)
    return all(results) if results else False


def advance(page) -> bool:
    """Close any open popup, click the wizard's next button (REFUSE 'submit'), then POLL
    for the active step to actually change (Workday saves async). Returns True only if it
    advanced. Uses an actionable locator/mouse click — NEVER force=True."""
    close_popups(page)
    page.mouse.click(5, 5)  # dismiss any lingering dropdown
    page.wait_for_timeout(400)
    nb = page.query_selector("[data-automation-id='pageFooterNextButton']")
    if not nb:
        return False
    txt = (nb.inner_text() or "").strip()
    if "submit" in txt.lower():
        # hard refusal — never click a control that submits the application
        return False
    before = active_step(page)
    try:
        nb.scroll_into_view_if_needed()
    except Exception:
        pass
    page.wait_for_timeout(300)
    try:
        page.get_by_role("button", name=txt, exact=False).first.click(timeout=4000)
    except Exception:
        box = nb.bounding_box()
        if box:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        else:
            return False
    # poll up to ~15s for the step to change (async save)
    for _ in range(15):
        page.wait_for_timeout(1000)
        if active_step(page) != before:
            return True
    return False
