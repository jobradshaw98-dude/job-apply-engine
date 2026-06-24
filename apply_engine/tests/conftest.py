import functools
import http.server
import threading
from pathlib import Path
import pytest

FIX = Path(__file__).parent / "fixtures"


def pytest_configure(config):
    # Opt-in marker for the slow, real-`claude -p` behavioural tests (test_ledger_prose_llm.py).
    # Registering it keeps `-m llm` deselection clean and silences the unknown-marker warning.
    config.addinivalue_line(
        "markers",
        "llm: slow test that shells out to the real Claude CLI; opt-in via `-m llm`.",
    )


@pytest.fixture(autouse=True)
def _isolate_manifest(tmp_path_factory, monkeypatch):
    """Redirect the staged-application manifest to a throwaway dir so orchestrator
    tests never write to the live ARIA data hub.

    Also isolates the corrections ledger: regen_answer now mirrors every answer edit into
    corrections_log, which appends to a file at the career root. Without this, exercising the
    real regen_answer entrypoint in tests would pollute the production ledger. corrections_log
    lives one level up from this package; import it the same way regen_answer does."""
    from apply_engine import config
    sandbox = tmp_path_factory.mktemp("aria_data")
    monkeypatch.setattr(config, "ARIA_DATA", sandbox)
    try:
        import corrections_log
        monkeypatch.setattr(corrections_log, "LEDGER_PATH", sandbox / "corrections_ledger.jsonl")
    except Exception:
        pass


@pytest.fixture(scope="session")
def fixture_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIX))
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
