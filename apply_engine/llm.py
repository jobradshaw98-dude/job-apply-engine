"""Real wiring for the answer drafter (Claude) and the fabrication gate (audit_gate.py),
plus the FACTS loader. Kept separate from answer_gen so that module stays pure/testable."""
import sys
import tempfile
from pathlib import Path

from . import config

CAREER_DIR = config.PKG_DIR.parent                  # repo root
BRIEF_CONFIG = config.ARIA_DATA / "brief_config.json"
RESUME = config.ARIA_DATA / "master_resume.md"
LEDGER = CAREER_DIR / "claims_ledger.md"
VOICE = config.PKG_DIR / "voice_profile.md"         # identity + craft (style, not facts)
# Capabilities + resume rules: prefer the user-supplied real file (git-ignored); fall back to the
# committed *.example.md (the fictional demo applicant) — mirrors VOICE/voice_profile.example.md.
CAPABILITIES = config.PKG_DIR / "capabilities.md"   # truthful Yes/No + AI-native coding framing
CAPABILITIES_EXAMPLE = config.PKG_DIR / "capabilities.example.md"
RESUME_RULES_FILE = config.PKG_DIR / "resume_rules.md"  # learned resume/deck rules (/career-learn)
RESUME_RULES_EXAMPLE = config.PKG_DIR / "resume_rules.example.md"


def _first_existing(*paths):
    """Return the first path that exists (user-supplied real file preferred), else the last one
    so a missing real file degrades to the committed example rather than vanishing."""
    for p in paths:
        if Path(p).exists():
            return p
    return paths[-1]
# Identity bedrock — the unfakeable themes the applicant writes high-stakes positioning FROM.
# User-supplied (gitignored); copy narrative.example.md → narrative.md and fill it in. Feeding it
# FIRST is the biggest lever on whether the drafter writes from who you are vs. generic boilerplate.
NARRATIVE = config.PKG_DIR / "narrative.md"


_WARNED_MISSING = set()


def _ground(parts: list, header: str, path, required_hint: str = "") -> bool:
    """Append a grounding file's contents under `header`. If it's missing, WARN LOUDLY on
    stderr (once per path) and continue — degraded grounding must be visible, never silent.
    This is the fix for the cold-clone blocker where a missing corpus produced generic essays
    with no signal as to why."""
    try:
        parts.append(header + Path(path).read_text(encoding="utf-8"))
        return True
    except Exception:
        key = str(path)
        if key not in _WARNED_MISSING:
            _WARNED_MISSING.add(key)
            extra = f" {required_hint}" if required_hint else ""
            print(f"[apply_engine] grounding file not found: {path} - drafted answers will be "
                  f"less targeted.{extra} See README 'Configure'.", file=sys.stderr)
        return False


def load_facts(job: dict = None, max_chars: int = 200000, recon_brief: str = "") -> str:
    """Grounding context for answer drafting.

    `recon_brief` (P3): the web-research brief from run_recon — company/role intel + fit mapping.
    It is fed as TARGETING guidance (what's true about the company and which of the applicant's real
    experiences map to the role), explicitly NOT a source of self-claims: the drafter still grounds
    every assertion about the applicant in their own corpus. This is what lets a corpus-only drafter write
    company-specific answers without web facts leaking in as fabricated personal claims.

    Order matters: NARRATIVE + VOICE first (who the applicant is + how they write), then the hard
    factual grounding (resume + vetted claims), then the JD for relevance. The
    VOICE/NARRATIVE blocks are style/identity guidance — clearly labelled NOT a source of
    factual claims — so answers read with cover-letter craft while every asserted fact
    still traces to the resume or claims ledger.

    Context budget (2026-06-17): the old 20k-char cap was the real quality bottleneck —
    voice (~10k) + capabilities (~6.5k) alone ate it, so the resume, the full claims ledger,
    and the JD were silently truncated away and the drafter wrote essays with almost none of
    the actual facts. The headless drafter runs on plan quota (zero API cost) and Claude holds
    1M tokens, so there is no reason to starve it. The cap is now a runaway-guard (~200k chars
    ≈ 50k tokens), not a real limit, and every grounding source is fed in full.
    """
    parts = []
    # Identity bedrock FIRST — write FROM who the applicant is, not generic boilerplate.
    _ground(parts,
            "# WHO THE APPLICANT IS (identity bedrock — the unfakeable themes to write FROM; "
            "style/positioning guidance, NOT a source of factual claims)\n",
            NARRATIVE, required_hint="Copy narrative.example.md -> narrative.md and fill it in.")
    _ground(parts,
            "# VOICE & CRAFT (how the applicant writes — style guidance, NOT a source "
            "of factual claims)\n",
            VOICE, required_hint="Copy voice_profile.example.md -> voice_profile.md and fill it in.")
    # Capability facts — what can be truthfully asserted, incl. the AI-native coding framing
    # (build/ship production systems via LLM harnesses; frame coding as AI-orchestrated, not
    # unaided fluency). Placed HIGH so it survives the max_chars cap.
    _ground(parts,
            "# CAPABILITY FACTS (truthful — incl. how to frame coding: AI-native, AI-orchestrated; "
            "assert shipped/operated systems, not unaided hand-coding)\n",
            _first_existing(CAPABILITIES, CAPABILITIES_EXAMPLE))
    try:
        _rr = _first_existing(RESUME_RULES_FILE,
                              RESUME_RULES_EXAMPLE).read_text(encoding="utf-8").strip()
        if _rr:
            parts.append("# LEARNED RESUME RULES (durable corrections distilled from past "
                         "edits — style/format guidance, NOT a source of factual claims)\n" + _rr)
    except Exception:
        pass  # optional/often-empty; absence is normal, no warning
    _ground(parts, "# RESUME (facts)\n", RESUME,
            required_hint="Set ARIA_CORE_DATA to a folder containing master_resume.md.")
    # Full claims ledger — the drafter must SEE every vetted claim to pick the strongest,
    # most role-relevant one. Fed in full.
    _ground(parts, "# VETTED CLAIMS — only assert what is supported here\n", LEDGER,
            required_hint="Add a claims_ledger.md at the repo root with your vetted claims.")
    if job and job.get("jd_text"):
        # FULL JD — real tailoring needs the whole posting (requirements, mission, team), not a
        # head slice. The blob is bounded by max_chars (a runaway guard), so feed the JD in full.
        parts.append("# JOB DESCRIPTION (for relevance only — not a source of facts)\n"
                     + str(job["jd_text"]))
    if recon_brief and recon_brief.strip():
        # Recon research brief: company/role intel + fit mapping for TARGETING. Explicitly NOT a
        # source of self-claims — every assertion about the applicant still traces to their own corpus above.
        parts.append("# RECON BRIEF (researched company/role intel + fit mapping — use ONLY to "
                     "target and tailor; NOT a source of factual claims about the applicant)\n"
                     + recon_brief.strip())
    return "\n\n".join(parts)[:max_chars]


class LLMUnavailable(RuntimeError):
    """Raised when generation can't run on the Claude subscription. We FAIL LOUD here rather
    than fall back to the metered Anthropic API — the user's hard rule is no surprise API spend."""


def make_claude_llm(model: str = "sonnet"):
    """Text generator for drafting/auditing answers.

    Runs ONLY on the user's Claude SUBSCRIPTION via Claude Code headless (`claude -p`), so
    background drafting and dashboard-triggered edits cost zero API tokens (plan quota only).
    There is intentionally NO Anthropic-API fallback: if the Claude Code CLI is missing or a
    call fails, we raise LLMUnavailable so nothing ever silently bills the API. (The API key
    in brief_config.json is deliberately untouched here.)
    """
    import shutil
    import subprocess
    cli = shutil.which("claude")
    if not cli:
        raise LLMUnavailable(
            "Claude Code CLI ('claude') not found on PATH. Generation runs on the plan via "
            "`claude -p`; refusing to fall back to the metered API. Install/login Claude Code.")

    def _fn(prompt: str) -> str:
        # `claude -p` cold-starts (CLI spin-up + hook cascade) can push a single
        # structured-JSON generation right up against the wall: a full tailor call was
        # measured at ~2.5 min, and a 240 s cap intermittently killed the whole run with
        # no recovery (observed 2026-06-12, JOB-307). So: a generous 420 s wall, and ONE
        # retry on timeout only (cold-start resilience). Non-timeout failures still raise
        # immediately, and there is still NO API fallback.
        last_timeout = None
        for _attempt in range(2):
            try:
                r = subprocess.run(
                    # --strict-mcp-config (with no --mcp-config) => this blind text generator
                    # spawns ZERO MCP servers. Without it, every headless draft call inherited the
                    # global MCP fleet (~8 stdio trees) it never uses, and a batch apply piled up
                    # hundreds of orphaned servers (the overheating leak, 2026-06-20). The agentic
                    # run_claude_agent path already passes this flag; this is the matching fix for
                    # the default single-call drafter.
                    [cli, "-p", "--output-format", "text", "--model", model,
                     "--strict-mcp-config"],
                    input=prompt, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=420,
                )
                break
            except subprocess.TimeoutExpired as e:
                last_timeout = e
                continue
            except Exception as e:  # noqa: BLE001
                raise LLMUnavailable(f"claude -p call failed ({type(e).__name__}: {e}); "
                                     "not falling back to the API.") from e
        else:
            raise LLMUnavailable(
                f"claude -p timed out twice ({last_timeout}); not falling back to the API."
            ) from last_timeout
        out = (r.stdout or "").strip()
        if not out:
            raise LLMUnavailable(
                "claude -p returned no text (exit "
                f"{r.returncode}); not falling back to the API. stderr: "
                f"{(r.stderr or '').strip()[:300]}")
        return out
    return _fn


# Optional extra grounding the recon/drafter agents may grep (read-only), scoped via --add-dir so
# an agent can read your past letters / narrative / notes without being handed the whole disk.
# All git-ignored and OPTIONAL: drop files in apply_engine/corpus/ to enable, or leave it absent.
# Each path is added only if it exists (see the `d.exists()` filters below), so a fresh clone with
# no corpus simply drafts from the FACTS string and the JD.
_CORPUS = config.PKG_DIR / "corpus"
_WRITING_BANK = _CORPUS / "writing-bank"     # example/past cover letters to match your cadence
_MEMORY_PROJECT = _CORPUS / "memory"          # narrative, project notes, feedback
_MEMORY_GLOBAL = _CORPUS / "memory-global"    # any second bucket you want greppable

# Read-only tool floor every agent gets. Mutating + shell tools are HARD-denied so a guarded agent
# can never write, edit, or run commands — it can only read/search and (for recon) browse.
_AGENT_READ_TOOLS = ["Read", "Grep", "Glob"]
_AGENT_WEB_TOOLS = ["WebSearch", "WebFetch"]
_AGENT_DENY_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput", "KillShell"]


# --- token-usage instrumentation (opt-in; off in normal runs so production stays on text output) ---
# When capture is enabled, run_claude_agent switches to `--output-format json` and accumulates the
# real input/output token counts the CLI reports, so we can measure per-job cost of the agent fleet.
_USAGE = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
_CAPTURE_USAGE = False


def enable_usage_capture():
    global _CAPTURE_USAGE
    _CAPTURE_USAGE = True
    reset_usage()


def disable_usage_capture():
    global _CAPTURE_USAGE
    _CAPTURE_USAGE = False


def reset_usage():
    _USAGE.update(input_tokens=0, output_tokens=0, calls=0)


def get_usage() -> dict:
    return dict(_USAGE)


def _accumulate_usage(stdout: str) -> str:
    """Parse a `claude -p --output-format json` reply: accumulate token usage, return the result
    text. Defensive — on any parse miss, return the raw stdout and count the call only."""
    import json as _json
    _USAGE["calls"] += 1
    try:
        obj = _json.loads(stdout)
        u = obj.get("usage", {}) or {}
        # cache reads/creates are input tokens too — count them so the measurement is honest.
        _USAGE["input_tokens"] += int(u.get("input_tokens", 0) or 0) \
            + int(u.get("cache_read_input_tokens", 0) or 0) \
            + int(u.get("cache_creation_input_tokens", 0) or 0)
        _USAGE["output_tokens"] += int(u.get("output_tokens", 0) or 0)
        return (obj.get("result") or "").strip()
    except Exception:  # noqa: BLE001
        return (stdout or "").strip()


def run_claude_agent(prompt: str, *, model: str = "sonnet", allow_web: bool = False,
                     allow_fetch: bool = True, system: str = "", add_dirs=(), timeout: int = 600) -> str:
    """A GUARDED agentic `claude -p` — the building block for the apply pipeline's agent fleet.

    Unlike make_claude_llm (a blind text generator), this lets the model REACH: it runs with
    read-only file tools (Read/Grep/Glob) so it can ground itself in the applicant's actual corpus, and
    — for the recon role only (allow_web=True) — WebSearch/WebFetch to research the company, role,
    and networking openings. Hard guardrails:
      • Mutating/shell tools (Write/Edit/Bash/...) are ALWAYS denied — an agent can never change a
        file or run a command. Read and (optionally) browse only.
      • --strict-mcp-config with no MCP config => the agent spawns NO MCP servers (avoids the
        session-pileup overheating issue) and has no access to ARIA's connectors.
      • Runs on the plan via `claude -p` — NO Anthropic-API fallback, same hard no-surprise-spend
        rule as make_claude_llm; raises LLMUnavailable on failure rather than billing the API.
      • Reach is SCOPED: cwd is the career dir and any extra context lives behind explicit --add-dir
        grants, not a bare filesystem.
    The model is bypassPermissions ONLY so the headless run never blocks on a tool prompt; the
    allow/deny tool lists — not the permission mode — are what actually bound what it can do.
    """
    import shutil
    import subprocess
    cli = shutil.which("claude")
    if not cli:
        raise LLMUnavailable(
            "Claude Code CLI ('claude') not found on PATH. Agents run on the plan via `claude -p`; "
            "refusing to fall back to the metered API. Install/login Claude Code.")
    # Web tools: WebSearch (cheap — snippets) and optionally WebFetch (expensive — pulls whole
    # pages). Lean recon allows search only (allow_fetch=False) to avoid the big page-fetch cost.
    web = []
    if allow_web:
        web = ["WebSearch"] + (["WebFetch"] if allow_fetch else [])
    allowed = list(_AGENT_READ_TOOLS) + web
    denied = list(_AGENT_DENY_TOOLS) + [t for t in _AGENT_WEB_TOOLS if t not in web]
    out_fmt = "json" if _CAPTURE_USAGE else "text"
    args = [cli, "-p", "--output-format", out_fmt, "--model", model,
            "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
            "--allowedTools", *allowed,
            "--disallowedTools", *denied]
    for d in add_dirs:
        args += ["--add-dir", str(d)]
    if system:
        args += ["--append-system-prompt", system]
    last_timeout = None
    for _attempt in range(2):  # one retry on timeout only (cold-start resilience)
        try:
            r = subprocess.run(
                args, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                cwd=str(CAREER_DIR),
            )
            break
        except subprocess.TimeoutExpired as e:
            last_timeout = e
            continue
        except Exception as e:  # noqa: BLE001
            raise LLMUnavailable(f"claude agent call failed ({type(e).__name__}: {e}); "
                                 "not falling back to the API.") from e
    else:
        raise LLMUnavailable(
            f"claude agent timed out twice ({last_timeout}); not falling back to the API."
        ) from last_timeout
    out = _accumulate_usage(r.stdout) if _CAPTURE_USAGE else (r.stdout or "").strip()
    if not out:
        raise LLMUnavailable(
            f"claude agent returned no text (exit {r.returncode}); not falling back to the API. "
            f"stderr: {(r.stderr or '').strip()[:300]}")
    return out


_DRAFTER_CONTRACT = (
    "You are drafting the applicant's own job-application answers — first person, their voice. "
    "The prompt gives you a FACTS block (their narrative, voice, resume, vetted claims, and the JD) "
    "as your floor. You ALSO have read-only tools (Read/Grep/Glob) and may REACH INTO their career "
    "corpus for anything that makes the answer stronger and more specific:\n"
    f"- Past cover letters / writing bank under: {_WRITING_BANK} (match their real cadence; reuse a "
    "turn of phrase or framing they have actually used — never copy whole passages).\n"
    f"- Their memory (narrative, project notes, feedback) under: {_MEMORY_PROJECT} and {_MEMORY_GLOBAL} "
    "(to pick the most role-relevant, truthful example).\n"
    "HARD RULES (never break, regardless of what you read):\n"
    "- Assert ONLY what the applicant's corpus supports. Never invent a tool, number, employer, metric, "
    "or outcome.\n"
    "- For a question about experience they lack EXACTLY (external customers, a specific language, an "
    "industry): do NOT blank-decline — give an honest answer leading with their closest real "
    "experience and candid about the gap. DECLINE only if answering would require inventing a fact, "
    "or the question asks for a personal commitment (location/relocation/office/start/pay).\n"
    "- The web is NOT available to you and must never be a source — ground every claim in their "
    "own corpus.\n"
    "- NEVER mention visa, citizenship, work authorization, sponsorship, relocation, start date, "
    "or pay. Those are handled elsewhere.\n"
    "- Output ONLY the answer text the prompt asks for (or DECLINE). Do not narrate what you read "
    "or explain your process.")


def make_claude_drafter(model: str = "sonnet"):
    """The agentic answer DRAFTER (P1 of the agent-fleet rebuild).

    Drop-in for make_claude_llm's text generator, but agentic: it keeps the full FACTS blob inline
    as a floor AND can reach the applicant's writing bank + memory (read-only, scoped, NO web) to ground a
    sharper, more voice-true answer — the reach that makes a live draft better than a blind one.
    Same plan-quota-only / no-API-fallback guarantee. Returns a callable(prompt) -> text so it slots
    into answer_gen.generate unchanged (draft + refine + critique + revise all gain corpus reach)."""
    import shutil
    # Detect the CLI at CONSTRUCTION (like make_claude_llm) so build_hooks degrades safely to
    # escalate-every-question when `claude` is absent, rather than handing back a callable that
    # only explodes mid-run on the first question.
    if not shutil.which("claude"):
        raise LLMUnavailable(
            "Claude Code CLI ('claude') not found on PATH. The agentic drafter runs on the plan "
            "via `claude -p`; refusing to fall back to the metered API. Install/login Claude Code.")
    dirs = [d for d in (_WRITING_BANK, _MEMORY_PROJECT, _MEMORY_GLOBAL) if d.exists()]

    def _fn(prompt: str) -> str:
        return run_claude_agent(prompt, model=model, allow_web=False,
                                system=_DRAFTER_CONTRACT, add_dirs=dirs, timeout=600)
    return _fn


_SINGLE_CALL_CONTRACT = (
    "You are drafting the applicant's own job-application answers — first person, their voice. You "
    "have read-only tools (Read/Grep/Glob) and may reach their career corpus (writing bank, memory) "
    "for the sharpest, most voice-true, role-relevant material. The web is NOT available.\n"
    "You will receive SEVERAL questions for ONE application. For EACH question, work internally: "
    "draft a strong answer, then self-critique it hard, then write the FINAL revised answer. Your "
    "self-critique must enforce:\n"
    "- A specific lead — open with substance, never throat-clearing or restating the question.\n"
    "- DIRECTNESS — answer the LITERAL question first and plainly; never bury the real answer in a "
    "metaphor, framing device, or abstract thesis. Concise beats over-written.\n"
    "- GROUNDED — assert ONLY what the FACTS support; a concrete grounded detail (a named project, "
    "a real metric, even a 'replaced a recurring ten-person cross-team review') is STRONGER than a "
    "vague claim. Never invent a tool, number, or employer.\n"
    "- RANGE — across the set, lead each answer with a DIFFERENT hero example so the whole "
    "application shows breadth, not one story retold.\n"
    "- HONEST REFRAME — for an experience they lack exactly (external customers, a language, an "
    "industry), give an honest grounded answer naming the closest real experience and candid about "
    "the gap; do NOT blank-decline.\n"
    "HARD RULES: NEVER mention visa/citizenship/work-authorization/sponsorship/relocation/start-"
    "date/pay. Output DECLINE for a question only when a truthful answer would require a fabricated "
    "fact or a personal commitment.\n"
    'Return STRICT JSON ONLY — no prose, no code fences: {"answers":[{"n":1,"text":"<final answer '
    'or DECLINE>"}, ...]} with exactly one entry per question, in the order given.')


def make_single_call_agent(model: str = "sonnet"):
    """The single-call drafter (2026-06-17): ONE guarded agentic call drafts + self-critiques +
    finalizes ALL of a job's answers at once. Measured equal-quality to the multi-pass pipeline at
    ~1/6 the calls and ~6x faster (it pays the ~34k fixed per-call overhead ONCE, reasons across the
    whole set for natural range, and reaches the corpus). Returns a callable(prompt)->text returning
    the JSON answer set; answer_gen.draft_single_call parses + gates it. CLI detected at construction
    so build_hooks degrades safely when `claude` is absent."""
    import shutil
    if not shutil.which("claude"):
        raise LLMUnavailable(
            "Claude Code CLI ('claude') not found on PATH. The single-call drafter runs on the plan "
            "via `claude -p`; refusing to fall back to the metered API. Install/login Claude Code.")
    dirs = [d for d in (_WRITING_BANK, _MEMORY_PROJECT, _MEMORY_GLOBAL) if d.exists()]

    def _fn(prompt: str) -> str:
        return run_claude_agent(prompt, model=model, allow_web=False,
                                system=_SINGLE_CALL_CONTRACT, add_dirs=dirs, timeout=700)
    return _fn


def _load_skill(name: str) -> str:
    """Load a markdown skill (apply_engine/skills/<name>.md), drop any YAML frontmatter, and
    return its body. Externalizes prompt/contract text out of inline Python constants so the
    same definition is reusable + versioned (migration step 3). The returned text is
    byte-identical to the former inline constant — guarded by tests/test_skills_recon.py."""
    text = (config.PKG_DIR / "skills" / f"{name}.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("\n---\n", 1)
        if len(parts) == 2:
            text = parts[1]
    return text.strip()


_RECON_CONTRACT = _load_skill("recon")
_LEAN_RECON_CONTRACT = _load_skill("recon-lean")


def run_recon(job: dict, model: str = "sonnet", timeout: int = 600, lean: bool = False) -> str:
    """P3: the web-enabled RECON agent. Researches company/role and returns a brief the (corpus-only)
    drafter consumes for company-specific targeting — it researches the TARGET, never writes the
    applicant's claims, so web facts inform relevance without becoming fabricated self-claims.

    lean=True: WebSearch snippets only (no expensive WebFetch page pulls), a tight ~200-word
    drafting brief (company/values/hooks) — drops the networking/comp/status sections the drafter
    never uses. Much cheaper; the right default when the brief is only feeding the answer drafter."""
    company = job.get("company") or job.get("employer") or ""
    title = job.get("title") or job.get("role") or ""
    url = job.get("url") or ""
    jd = str(job.get("jd_text") or "")
    prompt = (f"Company: {company}\nRole: {title}\nPosting URL: {url}\n\n"
              f"JOB DESCRIPTION:\n{jd}\n\nResearch this and produce the brief.")
    dirs = [d for d in (_MEMORY_PROJECT, _MEMORY_GLOBAL, _WRITING_BANK) if d.exists()]
    return run_claude_agent(prompt, model=model, allow_web=True,
                            allow_fetch=not lean,
                            system=_LEAN_RECON_CONTRACT if lean else _RECON_CONTRACT,
                            add_dirs=dirs, timeout=timeout)


# Em-dashes read as an AI tell, so an ANSWER carrying more than two of them is blocked here.
# This is the answer-path gate (drafting + regen + refresh_audit all route through make_audit_fn);
# it deliberately does NOT touch audit_gate.py's resume/cover bullet rule (run_on_emdash), which
# the user keeps as-is. Threshold > 2 (not > 1) leaves the one-or-zero target to the drafting
# prompt + voice profile while catching the egregious "em-dashes everywhere" case deterministically.
_MAX_ANSWER_EMDASHES = 2


def make_audit_fn():
    """Wrap career/audit_gate.py: return the list of BLOCK notes for an answer string.

    Adds an answer-only deterministic backstop: more than two em-dashes is a BLOCK ('an AI
    tell'). This covers answer drafting, dashboard regen, and refresh-audit — every path that
    gates an answer through this wrapper — without altering audit_gate's resume/cover rules."""
    if str(CAREER_DIR) not in sys.path:
        sys.path.insert(0, str(CAREER_DIR))
    from audit_gate import audit_file
    from .disclosure_guard import detect_immigration_disclosure

    def _fn(text: str):
        p = Path(tempfile.gettempdir()) / f"ag_{abs(hash(text)) % 999999}.html"
        p.write_text(f"<html><body><p>{text}</p></body></html>", encoding="utf-8")
        try:
            res = audit_file(str(p))
            # people-count framing ('replaced a recurring ten-person, two-hour cross-team review')
            # is ALLOWED in free-text ESSAY answers — a concrete, vivid claim is stronger than a
            # vague '~90%'. The ledger's percentage-only rule stays for RESUME/COVER bullets (clean,
            # scannable, consistent), which audit on the audit_gate path directly, NOT through this
            # answer wrapper. So we drop only the 'impact_as_count' block here (2026-06-17).
            notes = [v.get("note") or v.get("rule")
                     for v in res.get("violations", [])
                     if v.get("severity") == "block" and v.get("rule") != "impact_as_count"]
        finally:
            try:
                p.unlink()
            except Exception:
                pass
        n_dash = (text or "").count("—")
        if n_dash > _MAX_ANSWER_EMDASHES:
            notes.append(f"too many em-dashes ({n_dash}) — an AI tell; rewrite with "
                         "periods/commas")
        # Immigration/work-auth DISCLOSURE backstop (deterministic). A ledger-grounded answer can be
        # TRUTHFUL yet volunteer the applicant's visa/citizenship/sponsorship/GC status — the work-auth
        # policy forbids that in free-text content. Surfacing it as a gate BLOCK note makes the
        # answer-drafting + regen iterate-to-clean loops converge by REMOVING the disclosure (and it
        # also flows through refresh_audit's gate_fn). The structured findings in refresh_audit add
        # the category/offending_text the convergence loop + verify_ready key off.
        for dh in detect_immigration_disclosure(text):
            notes.append(
                f"immigration/work-auth disclosure ({dh.get('category', '')}): "
                f"\"{(dh.get('offending_text') or '')[:120]}\" — "
                + dh.get("fix", "REMOVE the visa/citizenship/sponsorship sentence; work "
                                "authorization is handled only in the structured screening fields"))
        return notes
    return _fn
