"""Per-job run directory: append-only JSONL audit log + screenshot capture.
Time is injected (caller passes a stamp) so the module is deterministic and testable.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunContext:
    job_id: str
    runs_root: Path
    stamp: str = "run"            # caller supplies a timestamp slug; default keeps tests stable
    _seq: int = field(default=0, init=False)
    _shot: int = field(default=0, init=False)

    def __post_init__(self):
        self.runs_root = Path(self.runs_root)
        self.run_dir = self.runs_root / f"{self.job_id}_{self.stamp}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.run_dir / "audit.jsonl"
        # Stable per-JOB progress file (not per-run-stamp) so the dashboard's Operations bar can
        # surface the live step without knowing the run stamp. Truncated fresh at run start; the
        # LAST line is the current step. Best-effort only — never load-bearing.
        self.progress_path = self.runs_root / f"{self.job_id}_progress.jsonl"
        try:
            self.progress_path.write_text("", encoding="utf-8")
        except Exception:
            pass

    def log(self, kind: str, message: str, **extra) -> None:
        self._seq += 1
        rec = {"seq": self._seq, "kind": kind, "message": message, **extra}
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # mirror to the stable progress file (phase = kind). Best-effort: a progress-write failure
        # must NEVER break the apply run, so it's swallowed.
        try:
            with self.progress_path.open("a", encoding="utf-8") as pf:
                pf.write(json.dumps({"seq": self._seq, "phase": kind, "message": message},
                                    ensure_ascii=False) + "\n")
        except Exception:
            pass

    def next_screenshot_path(self, label: str) -> Path:
        self._shot += 1
        return self.run_dir / f"step_{self._shot:02d}_{label}.png"
