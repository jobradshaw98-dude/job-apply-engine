"""Guard: externalizing the recon contracts into apply_engine/skills/*.md must be
BYTE-IDENTICAL to the former inline constants. _goldens.json captured the exact
strings at extraction time (migration step 3). If a skill file is edited in a way
that changes the assembled prompt, this fails."""
import json
from pathlib import Path

from apply_engine import llm

SKILLS = Path(llm.config.PKG_DIR) / "skills"
GOLDENS = json.loads((SKILLS / "_goldens.json").read_text(encoding="utf-8"))


def test_recon_skill_matches_golden():
    assert llm._load_skill("recon") == GOLDENS["recon"]


def test_recon_lean_skill_matches_golden():
    assert llm._load_skill("recon-lean") == GOLDENS["recon-lean"]


def test_module_constants_load_from_skills():
    # the live constants the recon agent uses are exactly the golden text
    assert llm._RECON_CONTRACT == GOLDENS["recon"]
    assert llm._LEAN_RECON_CONTRACT == GOLDENS["recon-lean"]


def test_loader_strips_frontmatter():
    body = llm._load_skill("recon")
    assert not body.startswith("---") and "name: recon" not in body.split("\n")[0]
