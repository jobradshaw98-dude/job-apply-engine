"""Hermetic tests for the engage reversibility spine + orchestrator.

NO network, NO model shell-out, NO browser, NO real data hub, NO real git side
effects on the repo under test. The autouse `_isolate_manifest` fixture (conftest)
already redirects config.ARIA_DATA to a throwaway dir; these tests write their
fixture jobs/contacts there.
"""
import json
import subprocess


from apply_engine import config
from apply_engine.engage import runner


# ── helpers ───────────────────────────────────────────────────────────────────
def _write(name, data):
    p = config.ARIA_DATA / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _base_cfg(**over):
    cfg = dict(runner._CONFIG_DEFAULTS)
    cfg.update(over)
    return cfg


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


# ── journal + bucketing ───────────────────────────────────────────────────────
def test_journal_written_with_bucket_counts():
    # a contact missing an id -> bucket A; a high-fit company with no contact -> bucket B
    _write("contacts.json", [{"name": "No Id Person", "company": "Acme"}])
    _write("jobs.json", [{"id": "JOB-1", "company": "Globex", "fit_score": 9,
                          "url": "https://job-boards.greenhouse.io/globex/jobs/1",
                          "description": "x" * 300}])
    run = runner.run_engage(dry_run=False, commit=False, cfg=_base_cfg())
    journal = run.last_journal
    assert journal.exists()
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["counts"][runner.BUCKET_A] >= 1   # id assigned
    assert payload["counts"][runner.BUCKET_B] >= 1   # Globex needs a contact (planned)
    # every entry carries the journal shape
    for e in payload["entries"]:
        assert set(e) >= {"file", "id", "field", "before", "after", "bucket", "reason"}


def test_dry_run_makes_zero_writes_and_zero_commit(monkeypatch):
    contacts = [{"name": "No Id Person", "company": "Acme"}]
    cpath = _write("contacts.json", contacts)
    before = cpath.read_text(encoding="utf-8")
    # guard: any attempt to shell out to git must blow the test up
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("git called in dry-run")))
    run = runner.run_engage(dry_run=True, commit=True, cfg=_base_cfg(commit=True))
    # contacts.json untouched despite the missing id that a live run would fix
    assert cpath.read_text(encoding="utf-8") == before
    # journal exists and is flagged dry-run; no sha
    assert run.last_journal.exists()
    assert ".dryrun" in run.last_journal.name
    assert run.last_sha is None


def test_commit_off_by_default_no_git_invoked(monkeypatch):
    _write("contacts.json", [{"name": "No Id Person", "company": "Acme"}])
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("git called with commit off")))
    run = runner.run_engage(dry_run=False, commit=False, cfg=_base_cfg(commit=False))
    assert run.last_sha is None  # commit disabled -> no git, no sha


def test_lane_fault_routes_to_C_without_aborting(monkeypatch):
    # make the outreach hygiene step raise; the run must still finish and journal the others
    _write("contacts.json", [{"name": "No Id Person", "company": "Acme"}])
    _write("jobs.json", [])

    def boom(run, cfg):
        raise RuntimeError("simulated lane fault")

    monkeypatch.setattr(runner, "hygiene_outreach", boom)
    # rebuild PHASE_A to pick up the patched function object
    monkeypatch.setattr(runner, "PHASE_A",
                        [runner.hygiene_contacts, runner.hygiene_applications, boom])
    run = runner.run_engage(dry_run=False, commit=False, cfg=_base_cfg())
    # the fault is journaled to bucket C, and the contacts hygiene (bucket A) still ran
    buckets = [e["bucket"] for e in run.entries]
    assert runner.BUCKET_C in buckets
    assert any(e["field"] == "error" and "simulated lane fault" in str(e["after"])
               for e in run.entries)
    assert runner.BUCKET_A in buckets  # earlier step still recorded


def test_malformed_config_fails_safe(tmp_path):
    # a malformed config file must NOT enable any lane — reverts to defaults
    (config.ARIA_DATA).mkdir(parents=True, exist_ok=True)
    (config.ARIA_DATA / "engage_config.json").write_text("{ this is not json", encoding="utf-8")
    cfg = runner.load_config()
    assert cfg == runner._CONFIG_DEFAULTS
    assert cfg["enable_sourcing"] is False
    assert cfg["commit"] is False


def test_changed_files_only_lists_bucket_A_json():
    run = runner.EngageRun(dry_run=False)
    run.record(file="contacts.json", target_id="CON-1", field="id", before=None,
               after="CON-1", bucket=runner.BUCKET_A, reason="x")
    run.record(file="staged_applications.json", target_id="JOB-1", field="stage-app",
               before=None, after="planned", bucket=runner.BUCKET_B, reason="x")
    run.record(file="(orchestrator)", target_id="boom", field="error", before=None,
               after="err", bucket=runner.BUCKET_C, reason="x")
    assert run.changed_files() == ["contacts.json"]


# ── opt-in commit, scoped to changed files only (real temp git repo) ───────────
def test_commit_scoped_to_changed_files_never_add_all(tmp_path, monkeypatch):
    # point ARIA_DATA at a real temp git repo so the commit path runs for real,
    # but only against this throwaway repo (never the repo under test).
    repo = tmp_path / "datarepo"
    repo.mkdir()
    monkeypatch.setattr(config, "ARIA_DATA", repo)
    assert _git(["init"], repo).returncode == 0
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)

    # a tracked state file the run will change, and an UNTRACKED "secret" that must
    # NOT be swept into the commit (this is the `git add -A` hazard).
    _write("contacts.json", [{"name": "No Id", "company": "Acme"}])
    (repo / "secrets.env").write_text("API_KEY=do-not-commit", encoding="utf-8")
    _git(["add", "contacts.json"], repo)
    _git(["commit", "-m", "seed"], repo)

    run = runner.run_engage(dry_run=False, commit=True, repo=repo, cfg=_base_cfg(commit=True))
    assert run.last_sha  # a commit happened

    # the secret is still untracked — the commit only touched changed state + journal
    status = _git(["status", "--porcelain"], repo).stdout
    assert "secrets.env" in status  # still untracked / uncommitted
    committed = _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo).stdout
    assert "secrets.env" not in committed
    assert "contacts.json" in committed


def test_commit_noop_outside_git_repo(tmp_path, monkeypatch):
    plain = tmp_path / "nogit"
    plain.mkdir()
    monkeypatch.setattr(config, "ARIA_DATA", plain)
    _write("contacts.json", [{"name": "No Id", "company": "Acme"}])
    run = runner.run_engage(dry_run=False, commit=True, repo=plain, cfg=_base_cfg(commit=True))
    assert run.last_sha is None  # not a git repo -> no commit, no crash
