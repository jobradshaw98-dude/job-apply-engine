"""Generic fallback filler for unknown ATSs. Maps each visible text-like input to an
answer key via field_map heuristics; fills what it can; records unmapped labels so the
orchestrator can flag low confidence. Work-auth detection + answering inherited from base."""
from typing import Dict, List
from .base import FormAdapterBase
from ..field_map import map_field

_TEXTLIKE = "input[type='text'], input[type='email'], input[type='tel'], input:not([type])"


class GenericFiller(FormAdapterBase):
    name = "generic"
    resume_selector = "input[type='file']"

    def __init__(self, llm_hook=None):
        self.llm_hook = llm_hook
        self.unmapped: List[str] = []
        self._filled: Dict[str, str] = {}   # answer_key -> css selector used

    def _label_for(self, page, el) -> str:
        eid = el.get_attribute("id")
        if eid:
            lab = page.query_selector(f"label[for='{eid}']")
            if lab:
                return (lab.inner_text() or "").strip()
        return el.get_attribute("aria-label") or el.get_attribute("placeholder") or ""

    def fill(self, page, answers) -> Dict[str, str]:
        intended: Dict[str, str] = {}
        for el in page.query_selector_all(_TEXTLIKE):
            label = self._label_for(page, el)
            key = map_field(label, el.get_attribute("name") or "",
                            el.get_attribute("placeholder") or "", self.llm_hook)
            if not key:
                if label:
                    self.unmapped.append(label)
                continue
            val = answers.get(key)
            if not val:
                continue
            eid = el.get_attribute("id")
            sel = f"#{eid}" if eid else None
            if not sel:
                continue
            page.fill(sel, str(val))
            intended[key] = str(val)
            self._filled[key] = sel
        if self.resume_selector and page.query_selector(self.resume_selector):
            page.set_input_files(self.resume_selector, str(answers.resume_pdf))
        return intended

    def read_back(self, page, keys: List[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key in keys:
            sel = self._filled.get(key)
            if sel and page.query_selector(sel):
                out[key] = page.input_value(sel)
        return out
